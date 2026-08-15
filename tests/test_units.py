"""Tests for the MW -> MWh unit conversion (build prompt section 3):
"hourly MW x 1 hour = MWh before any cost math."
"""

from pathlib import Path

import duckdb
import pytest


def mw_to_mwh(power_mw, duration_hours):
    return power_mw * duration_hours


def test_one_hour_at_constant_power_equals_same_number_of_mwh():
    # the whole point of hourly resolution: 1 hour * X MW == X MWh, numerically identical
    assert mw_to_mwh(power_mw=4.0, duration_hours=1.0) == 4.0
    assert mw_to_mwh(power_mw=5.25, duration_hours=1.0) == 5.25
    assert mw_to_mwh(power_mw=0.0, duration_hours=1.0) == 0.0


def test_conversion_scales_with_duration():
    # sanity check the general MW*h=MWh identity outside the hourly special case too
    assert mw_to_mwh(power_mw=4.0, duration_hours=0.25) == 1.0  # 15 minutes at 4 MW = 1 MWh
    assert mw_to_mwh(power_mw=4.0, duration_hours=24.0) == 96.0  # a full day at 4 MW = 96 MWh


def test_total_demand_mwth_used_directly_as_hourly_mwh():
    # total_demand_mwth in int_hourly_decision.py is a power figure (section 4's formula);
    # since every row represents exactly 1 hour, multiplying by cost-per-mwh directly (no
    # separate *1h step) is only valid because mw_to_mwh(power, 1.0) == power.
    base_load_mwth = 4.0
    extra_demand_mwth = 1.25
    total_demand_mwth = base_load_mwth + extra_demand_mwth
    assert mw_to_mwh(total_demand_mwth, duration_hours=1.0) == total_demand_mwth


# --- consistency check against the actual ingested pipeline output, if it has been run -----

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "celsio.duckdb"


@pytest.mark.skipif(not DB_PATH.exists(), reason="run ingestion + `dbt run` first (see README)")
def test_pipeline_hourly_rows_are_exactly_one_hour_apart():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        gaps = con.sql(
            """
            select distinct date_diff('minute', lag(ts_utc) over (order by ts_utc), ts_utc) as gap_minutes
            from main_intermediate.int_hourly_decision
            """
        ).df()
    finally:
        con.close()

    gap_minutes = set(gaps["gap_minutes"].dropna())
    assert gap_minutes == {60}, (
        f"expected every consecutive pair of rows to be exactly 60 minutes apart (so that "
        f"MW x 1h = MWh holds row-by-row without a separate duration column), found gaps: "
        f"{gap_minutes}"
    )
