"""Join price + weather, compute synthetic plant demand, and run the gas-vs-electric
hourly decision logic (build prompt sections 4 and 5) against ACTUAL historical prices.

Joins on ts_utc (the absolute instant), not ts_berlin -- see stg_prices.sql for why the
naive local timestamp is unsafe as a join key on the fall-back DST day.

Assumptions are loaded fresh from config/assumptions.yaml on every run (single source of
truth -- values must never be hardcoded here). Path is relative to the dbt invocation cwd,
which by convention is dbt_project/ (see README).
"""

import sys
from pathlib import Path

import numpy as np
import yaml

ASSUMPTIONS_PATH = Path("../config/assumptions.yaml")


def load_assumptions():
    with open(ASSUMPTIONS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key, entry in cfg.items():
        if isinstance(entry, dict):
            source = entry.get("source", "")
            if isinstance(source, str) and source.startswith("NOT FOUND"):
                print(f"[int_hourly_decision] WARNING: assumption '{key}' is unverified: {source}", file=sys.stderr)
    return cfg


def model(dbt, session):
    dbt.config(materialized="table")

    prices = dbt.ref("stg_prices").df()
    weather = dbt.ref("stg_weather").df()

    df = prices.merge(weather[["ts_utc", "temperature_c"]], on="ts_utc", how="inner")

    cfg = load_assumptions()
    reference_temp_c = cfg["reference_temperature_c"]["value"]
    base_load_mwth = cfg["plant_base_load_mwth"]["value"]
    temp_sensitivity = cfg["temp_sensitivity_coefficient_mwth_per_c"]["value"]
    gas_price = cfg["gas_price_eur_per_mwh"]["value"]
    gas_efficiency = cfg["gas_boiler_efficiency_pct"]["value"] / 100.0
    electric_efficiency = cfg["electric_heater_efficiency_pct"]["value"] / 100.0
    markup = cfg["electricity_markup_eur_per_mwh"]["value"]

    # section 4 demand formula: extra_demand = max(reference_temp - temp, 0) * sensitivity
    extra_demand_mwth = (reference_temp_c - df["temperature_c"]).clip(lower=0) * temp_sensitivity
    df["total_demand_mwth"] = base_load_mwth + extra_demand_mwth

    # section 5 decision logic
    df["gas_cost_per_mwh"] = gas_price / gas_efficiency
    df["electric_cost_per_mwh"] = (df["price_eur_per_mwh"] + markup) / electric_efficiency

    df["gas_total_cost_eur"] = df["gas_cost_per_mwh"] * df["total_demand_mwth"]
    df["electric_total_cost_eur"] = df["electric_cost_per_mwh"] * df["total_demand_mwth"]
    df["smart_switching_cost_eur"] = np.minimum(df["gas_total_cost_eur"], df["electric_total_cost_eur"])
    df["chosen_source"] = np.where(
        df["electric_total_cost_eur"] < df["gas_total_cost_eur"], "electric", "gas"
    )

    return df[
        [
            "ts_utc",
            "ts_berlin",
            "price_eur_per_mwh",
            "temperature_c",
            "total_demand_mwth",
            "gas_cost_per_mwh",
            "electric_cost_per_mwh",
            "gas_total_cost_eur",
            "electric_total_cost_eur",
            "smart_switching_cost_eur",
            "chosen_source",
        ]
    ]
