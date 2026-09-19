# syncwithgarmin

Uploads InBody CSV exports (LookinBody "date,Measurement device.,Weight(lb),..." format)
to Garmin Connect as body-composition weigh-ins, via
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect).

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/<you>/syncwithgarmin.git
cd syncwithgarmin
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Dry run first — nothing is sent to Garmin without `--upload`:

```bash
python sync_inbody.py ~/Downloads/InBody-20260901.csv
python sync_inbody.py ~/Downloads/InBody-20260901.csv --since 2026-08-01
python sync_inbody.py ~/Downloads/InBody-20260901.csv --since 2026-08-01 --upload
```

Options: `--since YYYY-MM-DD`, `--limit N` (most recent N), `--per-day last|first|all`,
`--delay SECONDS` (default 1.5s between uploads),
`--force` (bypass dedupe), `--reset-state`, `--replace` (see below).

### Fixing entries already uploaded

`--replace` deletes every existing Garmin weigh-in on each date it is about to write,
then re-uploads from the CSV. It prompts for confirmation and **also deletes weigh-ins
from other sources** (scale, manual entry) on those dates. Pair it with `--reset-state`
so the local state file doesn't skip the rows you want rewritten:

```bash
python sync_inbody.py ~/Downloads/InBody-20260901.csv --replace --reset-state --upload
```

Credentials come from `GARMIN_EMAIL` / `GARMIN_PASSWORD` if set, otherwise it prompts.
MFA is prompted interactively. OAuth tokens are cached in `~/.garminconnect`
(override with `GARMINTOKENS`) so later runs don't need the password.

## Field mapping

| InBody column | Garmin field | Note |
|---|---|---|
| `Weight(lb)` | `weight` | converted to kg |
| `Percent Body Fat(%)` | `percent_fat` | |
| `Skeletal Muscle Mass(lb)` | `muscle_mass` | kg; Garmin has one muscle field and SMM is the number on the InBody sheet |
| `BMI(kg/m²)` | `bmi` | |
| `Basal Metabolic Rate(kcal)` | `basal_met` | |
| `Visceral Fat Level(Level)` | `visceral_fat_rating` | InBody 1–20 scale vs Garmin 1–59 — number is passed through as-is |
| `Total Body Water(L)` | `percent_hydration` | `TBW / weight_kg * 100` |
| `date` | `timestamp` | `YYYYMMDDHHMMSS`, treated as local time |

Columns InBody left as `-` are omitted. `bone_mass` is never sent: Bone Mineral Content
is empty in these exports, and BMC is not the same quantity as Garmin's bone mass anyway.
Every row has Skeletal Muscle Mass, but only model 570/380 rows measure Total Body Water,
so `percent_hydration` is sent for those rows only.

Garmin's FIT weight-scale record has exactly one muscle field and no lean-mass field, so
SMM goes there. Note that a Garmin Index scale would populate that field with total muscle
mass — a much larger number — so mixing this script with an Index scale would produce a
discontinuous series.

## Duplicate protection

Two layers, both bypassable with `--force`:

1. `.synced.json` records every uploaded timestamp locally.
2. Before uploading, existing Garmin entries for the date range are fetched and a row is
   skipped if that calendar date already has a weight within 0.15 kg. This is a heuristic —
   two genuinely distinct same-day measurements at nearly identical weight will collapse to one.

To undo an upload, use `client.delete_weigh_in(weight_pk, cdate)` or delete the entry in
the Garmin Connect app.

## Disclaimer

Unofficial and unaffiliated with Garmin or InBody. It talks to Garmin Connect through
python-garminconnect, which uses Garmin's private web API — that API can change or
rate-limit without notice. It writes to (and with `--replace`, deletes from) your real
Garmin account, so always dry-run first. No warranty; see [LICENSE](LICENSE).

Your CSV exports and `.synced.json` are health data and are gitignored — keep them that way.

## License

[MIT](LICENSE)
