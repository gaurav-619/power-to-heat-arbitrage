"""9-cell payback sensitivity table (section 7): 3 CAPEX scenarios x 3 gas-price scenarios
(base gas price +/-20%). Never a single point payback number.

payback_years = capex_eur_per_mwth[scenario] * plant_base_load_mwth / annual_savings_eur

annual_savings_eur is recomputed per gas-price scenario (electric cost is unaffected by the
gas price, so only the "always gas" and therefore the switching comparison changes).
"""

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ASSUMPTIONS_PATH = Path("../config/assumptions.yaml")
GAS_PRICE_SCENARIOS = [("-20%", 0.8), ("base", 1.0), ("+20%", 1.2)]


def load_assumptions():
    with open(ASSUMPTIONS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def model(dbt, session):
    dbt.config(materialized="table")

    hourly = dbt.ref("int_hourly_decision").df()
    cfg = load_assumptions()

    base_gas_price = cfg["gas_price_eur_per_mwh"]["value"]
    gas_efficiency = cfg["gas_boiler_efficiency_pct"]["value"] / 100.0
    base_load_mwth = cfg["plant_base_load_mwth"]["value"]
    capex_scenarios = [
        ("low", cfg["capex_eur_per_mwth"]["low"]),
        ("mid", cfg["capex_eur_per_mwth"]["mid"]),
        ("high", cfg["capex_eur_per_mwth"]["high"]),
    ]

    demand = hourly["total_demand_mwth"]
    electric_total_cost = hourly["electric_total_cost_eur"]  # unaffected by gas price

    rows = []
    for gas_label, gas_multiplier in GAS_PRICE_SCENARIOS:
        scenario_gas_price = base_gas_price * gas_multiplier
        gas_cost_per_mwh = scenario_gas_price / gas_efficiency
        gas_total_cost = gas_cost_per_mwh * demand

        annual_always_gas = gas_total_cost.sum()
        annual_smart_switching = np.minimum(gas_total_cost, electric_total_cost).sum()
        annual_savings = annual_always_gas - annual_smart_switching

        for capex_label, capex_per_mwth in capex_scenarios:
            capex_total = capex_per_mwth * base_load_mwth
            payback_years = capex_total / annual_savings if annual_savings > 0 else float("inf")
            rows.append(
                {
                    "gas_price_scenario": gas_label,
                    "gas_price_eur_per_mwh": scenario_gas_price,
                    "capex_scenario": capex_label,
                    "capex_eur_per_mwth": capex_per_mwth,
                    "capex_total_eur": capex_total,
                    "annual_always_gas_cost_eur": annual_always_gas,
                    "annual_smart_switching_cost_eur": annual_smart_switching,
                    "annual_savings_eur": annual_savings,
                    "payback_years": payback_years,
                }
            )

    return pd.DataFrame(rows)
