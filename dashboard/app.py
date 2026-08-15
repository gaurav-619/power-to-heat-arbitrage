"""Streamlit dashboard for the industrial electrification decision prototype.

Run from the project root: streamlit run dashboard/app.py
Requires ingestion/fetch_prices.py, ingestion/fetch_weather.py and `dbt run` (from inside
dbt_project/) to have already been run at least once -- see README.md.
"""

from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "celsio.duckdb"
ASSUMPTIONS_PATH = ROOT / "config" / "assumptions.yaml"

st.set_page_config(page_title="Celsio Prototype -- Electrification Decision", layout="wide")


@st.cache_resource
def get_connection():
    return duckdb.connect(str(DB_PATH), read_only=True)


@st.cache_data
def load_table(_conn, table_name):
    return _conn.sql(f"select * from {table_name}").df()


@st.cache_data
def load_assumptions():
    with open(ASSUMPTIONS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


st.title("Industrial Electrification Decision Prototype")
st.caption(
    "Gas vs. electric heating switching decision for a synthetic German industrial plant, "
    "driven by real SMARD day-ahead prices and Open-Meteo weather history."
)

if not DB_PATH.exists():
    st.error(
        f"No DuckDB database found at `{DB_PATH.relative_to(ROOT)}`.\n\n"
        "Run the pipeline first:\n\n"
        "1. `python ingestion/fetch_prices.py`\n"
        "2. `python ingestion/fetch_weather.py`\n"
        "3. `cd dbt_project && dbt run --profiles-dir .`"
    )
    st.stop()

con = get_connection()

annual_costs = load_table(con, "main_marts.mart_annual_costs")
forecast_summary = load_table(con, "main_marts.mart_forecast_summary")
forecast_backtest = load_table(con, "main_marts.mart_forecast_backtest")
payback_table = load_table(con, "main_marts.mart_payback_table")
hourly_decision = load_table(con, "main_intermediate.int_hourly_decision")
assumptions = load_assumptions()

unverified = [
    key
    for key, entry in assumptions.items()
    if isinstance(entry, dict) and str(entry.get("source", "")).startswith("NOT FOUND")
]
if unverified:
    st.warning(f"Unverified assumption(s), needs manual verification: {', '.join(unverified)}")

# --- KPI row -----------------------------------------------------------------------------
annual_always_gas = annual_costs["daily_always_gas_cost_eur"].sum()
annual_smart = annual_costs["daily_smart_switching_cost_eur"].sum()
annual_savings = annual_always_gas - annual_smart
savings_pct = (annual_savings / annual_always_gas * 100) if annual_always_gas else 0.0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Annual cost -- always gas", f"EUR {annual_always_gas:,.0f}")
k2.metric("Annual cost -- smart switching", f"EUR {annual_smart:,.0f}")
k3.metric("Annual savings", f"EUR {annual_savings:,.0f}", f"{savings_pct:.1f}%")
electric_hours_pct = (hourly_decision["chosen_source"] == "electric").mean() * 100
k4.metric("Hours electric was cheaper", f"{electric_hours_pct:.1f}%")

st.caption(
    "Retrospective analysis using ACTUAL historical day-ahead prices over the full ingested "
    "year -- \"what would we have paid\" under each strategy, not a forward-looking forecast."
)

# --- Daily cost comparison -----------------------------------------------------------------
st.subheader("Daily cost: always-gas vs. smart-switching")
daily_chart = annual_costs.set_index("berlin_date")[
    ["daily_always_gas_cost_eur", "daily_smart_switching_cost_eur"]
].rename(
    columns={
        "daily_always_gas_cost_eur": "Always gas (EUR)",
        "daily_smart_switching_cost_eur": "Smart switching (EUR)",
    }
)
st.line_chart(daily_chart)

# --- 9-cell payback table -------------------------------------------------------------------
st.subheader("Payback sensitivity table (9-cell)")
st.caption(
    "payback_years = capex_eur_per_mwth[scenario] x plant_base_load_mwth / annual_savings_eur, "
    "across 3 CAPEX scenarios x 3 gas-price scenarios (base +/-20%). Never a single point number."
)
pivot = payback_table.pivot(index="capex_scenario", columns="gas_price_scenario", values="payback_years")
pivot = pivot.reindex(index=["low", "mid", "high"], columns=["-20%", "base", "+20%"])
st.dataframe(pivot.style.format("{:.2f} yrs"), use_container_width=True)
with st.expander("Full payback table detail"):
    st.dataframe(payback_table, use_container_width=True)

# --- Forecasting -----------------------------------------------------------------------------
st.subheader("Price forecasting: seasonal-naive vs. persistence")
st.caption(
    "Seasonal-naive = mean of the preceding 4 occurrences of the same Europe/Berlin weekday+hour. "
    "Persistence = same hour, previous day. Backtest excludes the first 4 weeks of the dataset."
)
row = forecast_summary.iloc[0]
f1, f2, f3, f4 = st.columns(4)
f1.metric("Seasonal-naive MAE", f"{row['seasonal_naive_mae_eur_per_mwh']:.2f} EUR/MWh")
f2.metric("Persistence MAE", f"{row['persistence_mae_eur_per_mwh']:.2f} EUR/MWh")
f3.metric("Seasonal-naive MAPE", f"{row['seasonal_naive_mape_pct']:.1f}%")
f4.metric("Persistence MAPE", f"{row['persistence_mape_pct']:.1f}%")
winner = "Persistence" if row["persistence_mae_eur_per_mwh"] < row["seasonal_naive_mae_eur_per_mwh"] else "Seasonal-naive"
st.info(f"**{winner}** wins on MAE over the {int(row['backtest_hours'])}-hour backtest window.")

backtest_window = forecast_backtest[forecast_backtest["in_backtest_window"]].dropna(
    subset=["seasonal_naive_forecast", "persistence_forecast"]
)
sample = backtest_window.sort_values("ts_berlin").tail(24 * 14)  # last 2 backtest weeks
forecast_chart = sample.set_index("ts_berlin")[
    ["price_eur_per_mwh", "seasonal_naive_forecast", "persistence_forecast"]
].rename(
    columns={
        "price_eur_per_mwh": "Actual",
        "seasonal_naive_forecast": "Seasonal-naive forecast",
        "persistence_forecast": "Persistence forecast",
    }
)
st.line_chart(forecast_chart)

# --- Assumptions ------------------------------------------------------------------------------
st.subheader("Assumptions")
assumption_rows = []
for key, entry in assumptions.items():
    if not isinstance(entry, dict):
        continue
    value = entry.get("value", entry.get("mid", entry))
    assumption_rows.append(
        {
            "field": key,
            "value": value,
            "confidence": entry.get("confidence", ""),
            "source": entry.get("source", ""),
        }
    )
st.dataframe(pd.DataFrame(assumption_rows), use_container_width=True, hide_index=True)

with st.expander("Hourly decision detail (sample)"):
    st.dataframe(hourly_decision.sort_values("ts_utc").head(200), use_container_width=True)
