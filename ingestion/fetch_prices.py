"""Fetch day-ahead electricity prices: SMARD filter 4169, DE-LU region, hourly resolution.

The docs page at https://smard.api.bund.dev/ renders no static schema (JS app, empty on
fetch) -- the shapes below were confirmed directly against the live API before writing
this script:
  - index endpoint:  {base}/chart_data/4169/DE-LU/index_hour.json
                      -> {"timestamps": [<utc_ms>, ...]}  one entry per ~7-day chunk
  - series endpoint: {base}/chart_data/4169/DE-LU/4169_DE-LU_hour_<chunk_ts>.json
                      -> {"meta_data": {...}, "series": [[utc_ms, value_or_null], ...]}

Since 1 Oct 2025 EPEX day-ahead auctions clear at 15-minute resolution, but SMARD still
publishes this same hourly index/series pair across that boundary -- we use the hourly
series for the entire date range so pre- and post-Oct-2025 data stay consistent.
"""

import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://www.smard.de/app"
FILTER_ID = "4169"
REGION = "DE-LU"
RESOLUTION = "hour"
PRICE_UNIT = "EUR/MWh"  # per SMARD documentation; the API response carries no unit field
BERLIN = ZoneInfo("Europe/Berlin")
CHUNK_SPAN_MS = 7 * 24 * 60 * 60 * 1000  # index chunks are ~7 days apart
PLAUSIBLE_MIN, PLAUSIBLE_MAX = -1000.0, 5000.0  # EUR/MWh sanity bounds (DE prices can go negative)

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"


def resolve_date_range():
    end_date = datetime.date.today() - datetime.timedelta(days=2)
    start_date = end_date - datetime.timedelta(days=365)
    print(
        f"[fetch_prices] Resolved date range: {start_date.isoformat()} to "
        f"{end_date.isoformat()} (Europe/Berlin calendar dates, inclusive)"
    )
    return start_date, end_date


def berlin_bounds_utc_ms(start_date, end_date):
    """Convert [start_date, end_date] Europe/Berlin calendar days into a half-open UTC ms range."""
    start_local = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=BERLIN)
    end_local = datetime.datetime.combine(
        end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=BERLIN
    )
    start_ms = int(start_local.astimezone(datetime.timezone.utc).timestamp() * 1000)
    end_ms = int(end_local.astimezone(datetime.timezone.utc).timestamp() * 1000)
    return start_ms, end_ms


def fetch_json(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_index():
    url = f"{BASE_URL}/chart_data/{FILTER_ID}/{REGION}/index_{RESOLUTION}.json"
    data = fetch_json(url)
    if "timestamps" not in data:
        print(f"[fetch_prices] UNEXPECTED index response shape. Keys found: {list(data.keys())}")
        sys.exit(1)
    return data["timestamps"]


def fetch_chunk(chunk_ts):
    url = f"{BASE_URL}/chart_data/{FILTER_ID}/{REGION}/{FILTER_ID}_{REGION}_{RESOLUTION}_{chunk_ts}.json"
    data = fetch_json(url)
    if "series" not in data:
        print(
            f"[fetch_prices] UNEXPECTED series response shape for chunk {chunk_ts}. "
            f"Keys found: {list(data.keys())}"
        )
        sys.exit(1)
    return data["series"]


def main():
    start_date, end_date = resolve_date_range()
    start_ms, end_ms = berlin_bounds_utc_ms(start_date, end_date)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    all_chunk_timestamps = fetch_index()
    needed_chunks = [
        ts for ts in all_chunk_timestamps if ts + CHUNK_SPAN_MS > start_ms and ts < end_ms
    ]
    if not needed_chunks:
        print("[fetch_prices] UNEXPECTED: no index chunks overlap the resolved date range.")
        sys.exit(1)

    rows = {}
    for chunk_ts in needed_chunks:
        series = fetch_chunk(chunk_ts)
        (RAW_DIR / f"prices_{chunk_ts}.json").write_text(json.dumps(series), encoding="utf-8")

        for ts, value in series:
            if ts < start_ms or ts >= end_ms:
                continue
            if value is None:
                bad_dt = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc)
                print(f"[fetch_prices] UNEXPECTED null price within target range at {bad_dt.isoformat()}")
                sys.exit(1)
            assert isinstance(value, (int, float)), f"Non-numeric price {value!r} at ts={ts}"
            assert PLAUSIBLE_MIN <= value <= PLAUSIBLE_MAX, (
                f"Price {value} {PRICE_UNIT} at ts={ts} outside plausible range "
                f"[{PLAUSIBLE_MIN}, {PLAUSIBLE_MAX}]"
            )
            rows[ts] = value

    sorted_rows = sorted(rows.items())

    out_path = PROCESSED_DIR / "prices.csv"
    with out_path.open("w", encoding="utf-8") as f:
        f.write("timestamp_utc_ms,price_eur_per_mwh\n")
        for ts, value in sorted_rows:
            f.write(f"{ts},{value}\n")

    print(f"[fetch_prices] Wrote {len(sorted_rows)} hourly rows to {out_path} (unit: {PRICE_UNIT})")


if __name__ == "__main__":
    main()
