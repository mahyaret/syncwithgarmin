#!/usr/bin/env python3
"""Upload InBody CSV exports (LookinBody) to Garmin Connect as body composition.

Dry run by default; pass --upload to actually write to your Garmin account.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from getpass import getpass
from pathlib import Path

LB_TO_KG = 0.45359237
STATE_FILE = Path(__file__).resolve().parent / ".synced.json"
TOKENSTORE = os.getenv("GARMINTOKENS", "~/.garminconnect")


def num(row: dict[str, str], key: str) -> float | None:
    """InBody uses '-' for fields a given device model didn't measure."""
    raw = (row.get(key) or "").strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class Measurement:
    ts: datetime
    weight: float  # kg
    percent_fat: float | None = None
    percent_hydration: float | None = None
    muscle_mass: float | None = None
    bmi: float | None = None
    basal_met: float | None = None
    visceral_fat_rating: float | None = None
    device: str = ""

    @property
    def key(self) -> str:
        return self.ts.strftime("%Y%m%d%H%M%S")

    def kwargs(self) -> dict[str, object]:
        out: dict[str, object] = {
            "timestamp": self.ts.isoformat(),
            "weight": round(self.weight, 2),
        }
        for name in (
            "percent_fat",
            "percent_hydration",
            "muscle_mass",
            "bmi",
            "basal_met",
            "visceral_fat_rating",
        ):
            value = getattr(self, name)
            if value is not None:
                out[name] = round(value, 2)
        return out


def parse_csv(path: Path) -> list[Measurement]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    measurements: list[Measurement] = []
    for row in rows:
        stamp = (row.get("date") or "").strip()
        weight_lb = num(row, "Weight(lb)")
        if len(stamp) != 14 or weight_lb is None:
            continue
        try:
            ts = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        except ValueError:
            continue

        weight_kg = weight_lb * LB_TO_KG
        tbw_l = num(row, "Total Body Water(L)")
        # Garmin has one muscle field; SMM is the number on the InBody sheet.
        muscle_lb = num(row, "Skeletal Muscle Mass(lb)")

        measurements.append(
            Measurement(
                ts=ts,
                weight=weight_kg,
                percent_fat=num(row, "Percent Body Fat(%)"),
                # TBW is litres of water ~= kg, so litres/kg of bodyweight is the ratio.
                percent_hydration=(tbw_l / weight_kg * 100) if tbw_l else None,
                muscle_mass=(muscle_lb * LB_TO_KG) if muscle_lb else None,
                bmi=num(row, "BMI(kg/m²)"),
                basal_met=num(row, "Basal Metabolic Rate(kcal)"),
                visceral_fat_rating=num(row, "Visceral Fat Level(Level)"),
                device=(row.get("Measurement device.") or "").strip(),
            )
        )

    measurements.sort(key=lambda m: m.ts)
    return measurements


def collapse_per_day(items: list[Measurement], keep: str) -> list[Measurement]:
    if keep == "all":
        return items
    by_day: dict[str, Measurement] = {}
    for m in items:
        day = m.ts.strftime("%Y-%m-%d")
        if day not in by_day or (keep == "last") == (m.ts > by_day[day].ts):
            by_day[day] = m
    return sorted(by_day.values(), key=lambda m: m.ts)


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text())["uploaded"])
    except (ValueError, KeyError, OSError):
        return set()


def save_state(keys: set[str]) -> None:
    STATE_FILE.write_text(json.dumps({"uploaded": sorted(keys)}, indent=1))


def remote_weights(client, start: str, end: str) -> dict[str, list[float]]:
    """calendarDate -> weights already in Garmin, in kg."""
    existing: dict[str, list[float]] = {}
    try:
        data = client.get_body_composition(start, end) or {}
    except Exception as exc:  # noqa: BLE001 - dedupe is best-effort, never fatal
        print(f"! could not read existing Garmin entries ({exc}); skipping dedupe")
        return existing

    for entry in data.get("dateWeightList") or []:
        day = entry.get("calendarDate")
        grams = entry.get("weight")
        if day and grams:
            existing.setdefault(day, []).append(grams / 1000.0)
    return existing


RATE_LIMIT_HELP = """
Every Garmin login route is rate limiting this IP (HTTP 429).

This is not a password problem. Wait before retrying: usually under an hour,
occasionally longer. Repeat attempts extend the block. Your cached tokens have
been left in place, so once the limit clears a normal run may authenticate
without touching Garmin's login endpoint at all.

Note: a "<strategy> returned 429" warning on its own is not fatal --
garminconnect tries several login routes and only some get limited. If you were
prompted for an MFA code despite those warnings, the login was progressing and
the code was worth entering.
""".strip()


def _snapshot(path: Path) -> Path | None:
    """Copy the tokenstore aside so a failed re-login can't lose it."""
    backup = path.with_name(path.name + ".bak")
    try:
        if path.is_dir():
            shutil.copytree(path, backup, dirs_exist_ok=True)
        else:
            shutil.copy2(path, backup)
    except OSError as exc:
        print(f"! could not back up {path} ({exc})")
        return None
    return backup


def _restore(path: Path, backup: Path | None) -> None:
    if backup is None or not backup.exists():
        return
    try:
        if backup.is_dir():
            shutil.copytree(backup, path, dirs_exist_ok=True)
        else:
            shutil.copy2(backup, path)
        print(f"restored cached tokens in {path}")
    except OSError as exc:
        print(f"! could not restore {path} from {backup} ({exc})")


def _discard(backup: Path | None) -> None:
    if backup is None:
        return
    if backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)
    else:
        backup.unlink(missing_ok=True)


def connect(relogin: bool):
    """Log in, preferring cached tokens and never destroying them on failure."""
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    store = Path(TOKENSTORE).expanduser()

    # Pass 1: cached tokens, deliberately with no credentials. Given a password,
    # garminconnect treats an API-rejected cache as poisoned and falls back to a
    # full SSO login -- and that fallback is what trips Garmin's per-IP 429.
    # With no password it simply reports the failure and leaves the cache alone.
    if store.exists() and not relogin:
        try:
            client = Garmin()
            client.login(tokenstore=str(store))
            print(f"Logged in from cached tokens ({store})")
            return client
        except GarminConnectTooManyRequestsError:
            print(f"\n{RATE_LIMIT_HELP}", file=sys.stderr)
            raise SystemExit(2) from None
        except Exception as exc:  # noqa: BLE001 - any failure just means "ask for a password"
            print(f"! cached tokens unusable ({exc})")

    # Pass 2: full credential login. This one can write the tokenstore, so keep
    # a copy until we know it succeeded.
    backup = _snapshot(store) if store.exists() else None

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ")
    password = os.getenv("GARMIN_PASSWORD") or getpass("Garmin password: ")
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA code (Garmin just sent one, check email): "),
    )
    try:
        client.login(tokenstore=str(store))
    except GarminConnectTooManyRequestsError:
        _restore(store, backup)
        _discard(backup)
        print(f"\n{RATE_LIMIT_HELP}", file=sys.stderr)
        raise SystemExit(2) from None
    except GarminConnectAuthenticationError as exc:
        _restore(store, backup)
        _discard(backup)
        print(f"\nGarmin rejected these credentials: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except GarminConnectConnectionError as exc:
        _restore(store, backup)
        _discard(backup)
        print(f"\nCould not reach Garmin Connect: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    _discard(backup)
    print(f"Logged in as {getattr(client, 'display_name', email)}")
    return client


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", type=Path, help="InBody CSV export")
    ap.add_argument("--upload", action="store_true", help="actually write to Garmin")
    ap.add_argument("--since", help="only rows on/after this date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="cap number of rows processed")
    ap.add_argument(
        "--per-day",
        choices=("all", "last", "first"),
        default="all",
        help="collapse multiple same-day measurements (default: all)",
    )
    ap.add_argument("--force", action="store_true", help="ignore dedupe checks")
    ap.add_argument(
        "--replace",
        action="store_true",
        help="DELETE existing Garmin weigh-ins on each target date, then re-upload",
    )
    ap.add_argument("--reset-state", action="store_true", help="clear .synced.json")
    ap.add_argument(
        "--relogin",
        action="store_true",
        help="skip cached tokens and log in with email/password",
    )
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between uploads")
    args = ap.parse_args()

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"cleared {STATE_FILE.name}")

    items = parse_csv(args.csv)
    if not items:
        print("No usable rows found.", file=sys.stderr)
        return 1

    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d")
        items = [m for m in items if m.ts >= cutoff]
    items = collapse_per_day(items, args.per_day)
    if args.limit:
        items = items[-args.limit :]

    print(f"{len(items)} measurement(s) {items[0].ts.date()} .. {items[-1].ts.date()}")

    if not args.upload:
        for m in items:
            print(f"  {m.ts:%Y-%m-%d %H:%M}  {m.kwargs()}")
        print("\nDry run. Re-run with --upload to send these to Garmin Connect.")
        return 0

    client = connect(args.relogin)

    skip_dedupe = args.force or args.replace
    state = set() if skip_dedupe else load_state()
    existing = (
        {}
        if skip_dedupe
        else remote_weights(
            client, str(items[0].ts.date()), str(items[-1].ts.date())
        )
    )

    days = sorted({m.ts.strftime("%Y-%m-%d") for m in items})
    if args.replace:
        print(
            f"\n--replace will DELETE every existing Garmin weigh-in on {len(days)} "
            f"date(s) ({days[0]} .. {days[-1]}) and re-upload from the CSV."
        )
        print("Any weigh-in on those dates from another source will also be deleted.")
        if input("Type 'replace' to continue: ").strip() != "replace":
            print("Aborted.")
            return 1

    purged: set[str] = set()
    sent, skipped, failed = 0, 0, 0
    for m in items:
        day = m.ts.strftime("%Y-%m-%d")
        if args.replace and day not in purged:
            purged.add(day)
            try:
                removed = client.delete_weigh_ins(day, delete_all=True)
                if removed:
                    print(f"- {day} deleted {removed} existing weigh-in(s)")
            except Exception as exc:  # noqa: BLE001 - keep going, then re-upload
                print(f"x {day} delete failed: {exc}")
        if m.key in state:
            skipped += 1
            continue
        if any(abs(w - m.weight) < 0.15 for w in existing.get(day, [])):
            print(f"= {m.ts:%Y-%m-%d %H:%M} already on Garmin ({m.weight:.1f} kg)")
            skipped += 1
            continue

        try:
            client.add_body_composition(**m.kwargs())
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"x {m.ts:%Y-%m-%d %H:%M} failed: {exc}")
            failed += 1
            continue

        print(f"+ {m.ts:%Y-%m-%d %H:%M}  {m.weight:.1f} kg  {m.percent_fat}% fat")
        state.add(m.key)
        existing.setdefault(day, []).append(m.weight)
        sent += 1
        save_state(state)
        time.sleep(args.delay)

    print(f"\nuploaded {sent}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
