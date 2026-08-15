"""Fetch hourly temperature from Open-Meteo's historical archive API.

Endpoint: https://archive-api.open-meteo.com/v1/archive
Params: latitude=49.48, longitude=8.45, start_date, end_date, hourly=temperature_2m
No API key required.

Ludwigshafen is an ASSUMED proxy for "a generic German industrial site"

No `timezone` param is passed, so Open-Meteo returns times in GMT (UTC, zero offset) --
confirmed against a live response (`"timezone": "GMT", "utc_offset_seconds": 0`). Those
timestamps are treated as UTC-naive and localized to UTC before the Europe/Berlin join.

IMPORTANT: `start_date`/`end_date` are interpreted by Open-Meteo as GMT/UTC calendar days,
NOT Europe/Berlin calendar days. Berlin is UTC+1/+2, so requesting the Berlin-aligned dates
directly would silently clip up to 2 hours off each end of the range relative to
fetch_prices.py's Europe/Berlin-bounded window. We request a 1-day-padded UTC window and
then trim precisely down to the Europe/Berlin range ourselves, so the two series cover
exactly the same Europe/Berlin hours before they are ever joined.
"""

import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
LATITUDE = 49.48
LONGITUDE = 8.45
TEMP_UNIT = "°C"
PLAUSIBLE_MIN, PLAUSIBLE_MAX = -40.0, 50.0  # deg C sanity bounds for this latitude
BERLIN = ZoneInfo("Europe/Berlin")
UTC = datetime.timezone.utc

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"


def resolve_date_range():
    end_date = datetime.date.today() - datetime.timedelta(days=2)
    start_date = end_date - datetime.timedelta(days=365)
    print(
        f"[fetch_weather] Resolved date range: {start_date.isoformat()} to "
        f"{end_date.isoformat()} (Europe/Berlin calendar dates, inclusive)"
    )
    return start_date, end_date


def berlin_bounds_utc(start_date, end_date):
    """Half-open [start, end) UTC instant bounds for the Europe/Berlin calendar-day range."""
    start_local = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=BERLIN)
    end_local = datetime.datetime.combine(
        end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=BERLIN
    )
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def fetch_range(fetch_start_date, fetch_end_date):
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": fetch_start_date.isoformat(),
        "end_date": fetch_end_date.isoformat(),
        "hourly": "temperature_2m",
    }
    resp = requests.get(BASE_URL, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def main():
    start_date, end_date = resolve_date_range()
    start_utc, end_utc = berlin_bounds_utc(start_date, end_date)

    # pad by 1 day on each side so the UTC-calendar-day request fully covers the
    # Europe/Berlin-calendar-day range regardless of the +1/+2h offset
    fetch_start_date = start_date - datetime.timedelta(days=1)
    fetch_end_date = end_date + datetime.timedelta(days=1)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    data = fetch_range(fetch_start_date, fetch_end_date)
    (RAW_DIR / "weather.json").write_text(json.dumps(data), encoding="utf-8")

    if "hourly" not in data or "hourly_units" not in data:
        print(f"[fetch_weather] UNEXPECTED response shape. Top-level keys found: {list(data.keys())}")
        sys.exit(1)
    if "time" not in data["hourly"] or "temperature_2m" not in data["hourly"]:
        print(f"[fetch_weather] UNEXPECTED hourly block shape. Keys found: {list(data['hourly'].keys())}")
        sys.exit(1)

    actual_unit = data["hourly_units"]["temperature_2m"]
    if actual_unit != TEMP_UNIT:
        print(f"[fetch_weather] UNEXPECTED temperature unit: {actual_unit!r} (expected {TEMP_UNIT!r})")
        sys.exit(1)
    if data.get("timezone") != "GMT" or data.get("utc_offset_seconds") != 0:
        print(
            f"[fetch_weather] UNEXPECTED timezone in response: "
            f"timezone={data.get('timezone')!r} utc_offset_seconds={data.get('utc_offset_seconds')!r} "
            f"(expected GMT / 0 -- timestamps would no longer be safely UTC-naive)"
        )
        sys.exit(1)

    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    if len(times) != len(temps):
        print(f"[fetch_weather] UNEXPECTED: time array len {len(times)} != temperature array len {len(temps)}")
        sys.exit(1)

    out_path = PROCESSED_DIR / "weather.csv"
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        f.write("timestamp_utc_naive,temperature_c\n")
        for t, v in zip(times, temps):
            ts_utc = datetime.datetime.fromisoformat(t).replace(tzinfo=UTC)
            if ts_utc < start_utc or ts_utc >= end_utc:
                continue  # outside the Europe/Berlin-aligned range; padding artifact
            if v is None:
                print(f"[fetch_weather] UNEXPECTED null temperature at {t}")
                sys.exit(1)
            assert isinstance(v, (int, float)), f"Non-numeric temperature {v!r} at {t}"
            assert PLAUSIBLE_MIN <= v <= PLAUSIBLE_MAX, (
                f"Temperature {v} {TEMP_UNIT} at {t} outside plausible range "
                f"[{PLAUSIBLE_MIN}, {PLAUSIBLE_MAX}]"
            )
            f.write(f"{t},{v}\n")
            written += 1

    print(f"[fetch_weather] Wrote {written} hourly rows to {out_path} (unit: {TEMP_UNIT})")


if __name__ == "__main__":
    main()
