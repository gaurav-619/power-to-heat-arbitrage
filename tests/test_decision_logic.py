"""Tests for the gas-vs-electric hourly decision logic (build prompt section 5).

Formula under test (from config/assumptions.yaml, reproduced independently here so this
test still catches a regression if dbt_project/models/intermediate/int_hourly_decision.py
drifts from the spec):

    extra_demand_mwth   = max(reference_temp_c - temperature_c, 0) * temp_sensitivity
    total_demand_mwth   = base_load_mwth + extra_demand_mwth
    gas_cost_per_mwh     = gas_price_eur_per_mwh / gas_boiler_efficiency
    electric_cost_per_mwh = (day_ahead_price + electricity_markup) / electric_heater_efficiency
    gas_total_cost_eur     = gas_cost_per_mwh * total_demand_mwth
    electric_total_cost_eur = electric_cost_per_mwh * total_demand_mwth

Constants (config/assumptions.yaml): reference_temp_c=15.0, base_load_mwth=4.0,
temp_sensitivity=0.05, gas_price=38.21, gas_efficiency=0.88, electric_efficiency=0.99,
markup=24.85.
"""

import sys
from pathlib import Path

import duckdb
import pytest

REFERENCE_TEMP_C = 15.0
BASE_LOAD_MWTH = 4.0
TEMP_SENSITIVITY = 0.05
GAS_PRICE_EUR_PER_MWH = 38.21
GAS_EFFICIENCY = 0.88
ELECTRIC_EFFICIENCY = 0.99
MARKUP_EUR_PER_MWH = 24.85

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "celsio.duckdb"


def compute_hour(price_eur_per_mwh, temperature_c):
    """Independent reimplementation of the section-4/5 formulas for testing."""
    extra_demand_mwth = max(REFERENCE_TEMP_C - temperature_c, 0.0) * TEMP_SENSITIVITY
    total_demand_mwth = BASE_LOAD_MWTH + extra_demand_mwth

    gas_cost_per_mwh = GAS_PRICE_EUR_PER_MWH / GAS_EFFICIENCY
    electric_cost_per_mwh = (price_eur_per_mwh + MARKUP_EUR_PER_MWH) / ELECTRIC_EFFICIENCY

    gas_total_cost = gas_cost_per_mwh * total_demand_mwth
    electric_total_cost = electric_cost_per_mwh * total_demand_mwth
    return total_demand_mwth, gas_total_cost, electric_total_cost


# --- 4 hand-computed example hours (2 gas-favoring, 2 electric-favoring) -------------------

def test_gas_favoring_hour_cold_high_price():
    # temp=-5.0C -> extra_demand=(15-(-5))*0.05=1.0 -> total_demand=5.0
    # gas_cost_per_mwh=38.21/0.88=43.420454545454545 -> gas_total=217.10227272727272
    # electric_cost_per_mwh=(250+24.85)/0.99=277.6262626262626... -> electric_total=1388.131313...
    total_demand, gas_total, electric_total = compute_hour(price_eur_per_mwh=250.0, temperature_c=-5.0)
    assert total_demand == 5.0
    assert gas_total == pytest.approx(38.21 / 0.88 * 5.0)
    assert electric_total == pytest.approx((250.0 + 24.85) / 0.99 * 5.0)
    assert gas_total < electric_total, "expected gas to be the cheaper option"


def test_gas_favoring_hour_mild_high_price():
    # temp=20.0C (>= reference) -> extra_demand=0 -> total_demand=4.0
    # gas_total=43.420454545454545*4=173.68181818181819
    # electric_cost_per_mwh=(100+24.85)/0.99=126.11111111111111 -> electric_total=504.44444444444446
    total_demand, gas_total, electric_total = compute_hour(price_eur_per_mwh=100.0, temperature_c=20.0)
    assert total_demand == 4.0
    assert gas_total == pytest.approx(38.21 / 0.88 * 4.0)
    assert electric_total == pytest.approx((100.0 + 24.85) / 0.99 * 4.0)
    assert gas_total < electric_total, "expected gas to be the cheaper option"


def test_electric_favoring_hour_negative_price():
    # temp=10.0C -> extra_demand=(15-10)*0.05=0.25 -> total_demand=4.25
    # gas_total=43.420454545454545*4.25=184.53693181818182
    # electric_cost_per_mwh=(-10+24.85)/0.99=15.0 -> electric_total=63.75
    total_demand, gas_total, electric_total = compute_hour(price_eur_per_mwh=-10.0, temperature_c=10.0)
    assert total_demand == 4.25
    assert gas_total == pytest.approx(38.21 / 0.88 * 4.25)
    assert electric_total == pytest.approx(15.0 * 4.25)
    assert electric_total < gas_total, "expected electric to be the cheaper option"


def test_electric_favoring_hour_at_reference_temp():
    # temp=15.0C (== reference, not <) -> extra_demand=0 -> total_demand=4.0
    # gas_total=43.420454545454545*4=173.68181818181819
    # electric_cost_per_mwh=(5+24.85)/0.99=30.15151515151515 -> electric_total=120.60606060606061
    total_demand, gas_total, electric_total = compute_hour(price_eur_per_mwh=5.0, temperature_c=15.0)
    assert total_demand == 4.0
    assert gas_total == pytest.approx(38.21 / 0.88 * 4.0)
    assert electric_total == pytest.approx((5.0 + 24.85) / 0.99 * 4.0)
    assert electric_total < gas_total, "expected electric to be the cheaper option"


# --- sanity assertion over the full ingested pipeline output -------------------------------

@pytest.mark.skipif(not DB_PATH.exists(), reason="run ingestion + `dbt run` first (see README)")
def test_smart_switching_never_more_expensive_than_always_gas_any_day():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        offending = con.sql(
            """
            select berlin_date, daily_always_gas_cost_eur, daily_smart_switching_cost_eur
            from main_marts.mart_annual_costs
            where daily_smart_switching_cost_eur > daily_always_gas_cost_eur
            order by berlin_date
            """
        ).df()
    finally:
        con.close()

    if len(offending) > 0:
        for _, row in offending.iterrows():
            print(
                f"[test_decision_logic] BUG: {row['berlin_date']} smart_switching_cost="
                f"{row['daily_smart_switching_cost_eur']:.2f} > always_gas_cost="
                f"{row['daily_always_gas_cost_eur']:.2f}",
                file=sys.stderr,
            )
    assert len(offending) == 0, f"{len(offending)} day(s) violate the smart-switching sanity assertion"
