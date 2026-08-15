"""Tests for Europe/Berlin timezone handling (build prompt section 3).

Confirms, via DuckDB directly (the same `AT TIME ZONE` mechanism used in
dbt_project/models/staging/stg_prices.sql and stg_weather.sql), that a continuous UTC
hourly grid produces exactly 23 local hours on the spring-forward day and exactly 25 local
hours on the fall-back day when grouped by Europe/Berlin calendar date.

Germany/EU DST rule: clocks change on the last Sunday of March (spring forward, lose an
hour) and the last Sunday of October (fall back, gain an hour). Within our actual ingestion
window (see ingestion/fetch_prices.py, most recent full 12 months) that lands on:
  - 2025-10-26 (fall back)  -> 25 local hours
  - 2026-03-29 (spring forward) -> 23 local hours
"""

import datetime
from pathlib import Path

import duckdb
import pytest

FALL_BACK_DATE = datetime.date(2025, 10, 26)
SPRING_FORWARD_DATE = datetime.date(2026, 3, 29)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "celsio.duckdb"


def berlin_local_hour_count(con, local_date, window_hours=30, pad_hours=3):
    """Build a continuous UTC hourly grid spanning `local_date` and count distinct
    Europe/Berlin local hours that fall on that calendar date -- exercising the identical
    `AT TIME ZONE` conversion used in the dbt staging models."""
    utc_start = datetime.datetime.combine(
        local_date, datetime.time.min, tzinfo=datetime.timezone.utc
    ) - datetime.timedelta(hours=pad_hours)
    result = con.sql(
        """
        select count(*) as n
        from (
            select ?::timestamptz + interval (i) hour as ts_utc
            from range(?) as t(i)
        )
        where cast((ts_utc at time zone 'Europe/Berlin') as date) = ?
        """,
        params=[utc_start, window_hours, local_date],
    ).fetchone()
    return result[0]


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect()
    yield connection
    connection.close()


def test_fall_back_day_has_25_local_hours(con):
    count = berlin_local_hour_count(con, FALL_BACK_DATE)
    assert count == 25, f"expected 25 local hours on fall-back day {FALL_BACK_DATE}, got {count}"


def test_spring_forward_day_has_23_local_hours(con):
    count = berlin_local_hour_count(con, SPRING_FORWARD_DATE)
    assert count == 23, f"expected 23 local hours on spring-forward day {SPRING_FORWARD_DATE}, got {count}"


def test_ordinary_day_has_24_local_hours(con):
    ordinary_date = datetime.date(2026, 1, 15)
    count = berlin_local_hour_count(con, ordinary_date)
    assert count == 24, f"expected 24 local hours on an ordinary day {ordinary_date}, got {count}"


# --- consistency check against the actual ingested pipeline output, if it has been run -----

@pytest.mark.skipif(not DB_PATH.exists(), reason="run ingestion + `dbt run` first (see README)")
def test_pipeline_output_matches_expected_dst_day_row_counts():
    pcon = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        counts = {
            row[0]: row[1]
            for row in pcon.sql(
                """
                select cast(ts_berlin as date) as d, count(*) as n
                from main_intermediate.int_hourly_decision
                where cast(ts_berlin as date) in (?, ?)
                group by 1
                """,
                params=[FALL_BACK_DATE, SPRING_FORWARD_DATE],
            ).fetchall()
        }
    finally:
        pcon.close()

    assert counts.get(FALL_BACK_DATE) == 25, (
        f"expected 25 rows on fall-back day {FALL_BACK_DATE} in the real pipeline output, "
        f"got {counts.get(FALL_BACK_DATE)}"
    )
    assert counts.get(SPRING_FORWARD_DATE) == 23, (
        f"expected 23 rows on spring-forward day {SPRING_FORWARD_DATE} in the real pipeline "
        f"output, got {counts.get(SPRING_FORWARD_DATE)}"
    )
