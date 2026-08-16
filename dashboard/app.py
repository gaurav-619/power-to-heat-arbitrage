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

GAS_COLOR = "#C4622D"
ELECTRIC_COLOR = "#157A9E"

st.set_page_config(
    page_title="Celsio Prototype -- Electrification Decision", page_icon="⚡", layout="wide"
)


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

# --- shared figures, computed once from the real mart values -------------------------------
annual_always_gas = annual_costs["daily_always_gas_cost_eur"].sum()
annual_smart = annual_costs["daily_smart_switching_cost_eur"].sum()
annual_savings = annual_always_gas - annual_smart
savings_pct = (annual_savings / annual_always_gas * 100) if annual_always_gas else 0.0
electric_hours_pct = (hourly_decision["chosen_source"] == "electric").mean() * 100
payback_min = payback_table["payback_years"].min()
payback_max = payback_table["payback_years"].max()
date_min = pd.to_datetime(hourly_decision["ts_berlin"]).min().date()
date_max = pd.to_datetime(hourly_decision["ts_berlin"]).max().date()

# --- 6. plain-language summary, built from the values above, not hardcoded -----------------
with st.container(border=True):
    st.markdown(
        f"Based on real electricity price data from **{date_min:%d %b %Y}** to **{date_max:%d %b %Y}**, "
        f"switching to electric heating for the cheapest hours would have saved approximately "
        f"**EUR {annual_savings:,.0f} per year** ({savings_pct:.1f}%), with a payback period of "
        f"**{payback_min:.1f}-{payback_max:.1f} years** depending on installation cost and gas price "
        f"assumptions. Electricity was the cheaper option in **{electric_hours_pct:.1f}%** of hours."
    )

# --- 1 & 2. headline framing: the single number, then the range, front and center ----------
h1, h2, h3 = st.columns([1.3, 1.3, 1])
h1.metric("Annual savings", f"EUR {annual_savings:,.0f}", f"{savings_pct:.1f}% vs. always-gas")
h2.metric("Payback period", f"{payback_min:.1f}-{payback_max:.1f} yrs", "range across 9 scenarios", delta_color="off")
h3.metric("Hours electric was cheaper", f"{electric_hours_pct:.1f}%")

st.divider()

# --- 7. navigation: tabs so a reviewer can get the story from tab 1 alone ------------------
tab_overview, tab_payback, tab_hourly, tab_forecast, tab_assumptions = st.tabs(
    ["Overview", "Payback analysis", "Hourly detail", "Forecast accuracy", "Assumptions"]
)

with tab_overview:
    st.caption(
        "Retrospective analysis using ACTUAL historical day-ahead prices over the full ingested "
        "year -- \"what would we have paid\" under each strategy, not a forward-looking forecast."
    )

    # --- 4. chart clarity: when did the plant switch, month by month ------------------------
    st.subheader("When the plant would have switched to electric")
    monthly = hourly_decision.copy()
    monthly["month"] = pd.to_datetime(monthly["ts_berlin"]).dt.to_period("M").astype(str)
    monthly_counts = (
        monthly.groupby(["month", "chosen_source"]).size().unstack(fill_value=0)
    )
    for col in ("gas", "electric"):
        if col not in monthly_counts.columns:
            monthly_counts[col] = 0
    monthly_counts = monthly_counts[["gas", "electric"]]
    st.bar_chart(monthly_counts, color=[GAS_COLOR, ELECTRIC_COLOR])
    st.caption(
        "Hours per month where gas (orange) vs. electric (blue) was the cheaper option. "
        "Electric-heavy months line up with the low- and negative-price events described below."
    )

    st.subheader("Daily cost: always-gas vs. smart-switching")
    daily_chart = annual_costs.set_index("berlin_date")[
        ["daily_always_gas_cost_eur", "daily_smart_switching_cost_eur"]
    ].rename(
        columns={
            "daily_always_gas_cost_eur": "Always gas (EUR)",
            "daily_smart_switching_cost_eur": "Smart switching (EUR)",
        }
    )
    st.line_chart(daily_chart, color=[GAS_COLOR, ELECTRIC_COLOR])

with tab_payback:
    st.subheader("Payback range")
    st.caption(
        "payback_years = capex_eur_per_mwth[scenario] x plant_base_load_mwth / annual_savings_eur, "
        "across 3 CAPEX scenarios x 3 gas-price scenarios (base +/-20%). This is a range, not a "
        "single confident number -- treat any one cell as a scenario, not a forecast."
    )
    st.metric("Range across all 9 scenarios", f"{payback_min:.1f} - {payback_max:.1f} years")

    def payback_cell_color(value):
        # manual light->dark green interpolation (shorter payback = darker = better) --
        # avoids Styler.background_gradient, which pulls in matplotlib and isn't in the
        # project's fixed dependency set
        span = payback_max - payback_min
        frac_short = (payback_max - value) / span if span else 0.5
        light, dark = (0xEE, 0xF4, 0xEF), (0x9C, 0xC9, 0xA5)
        r, g, b = (int(light[i] + (dark[i] - light[i]) * frac_short) for i in range(3))
        return f"background-color: rgb({r},{g},{b}); color: #23422C"

    pivot = payback_table.pivot(index="capex_scenario", columns="gas_price_scenario", values="payback_years")
    pivot = pivot.reindex(index=["low", "mid", "high"], columns=["-20%", "base", "+20%"])
    st.dataframe(
        pivot.style.format("{:.2f} yrs").map(payback_cell_color),
        width="stretch",
    )
    st.caption("Darker = shorter payback (more favorable). Rows: CAPEX scenario. Columns: gas-price scenario.")

    with st.expander("Full 9-row detail"):
        st.dataframe(payback_table, width="stretch")

with tab_hourly:
    st.subheader("Hourly decision detail")
    st.caption("Every ingested hour, with the actual cost of each option and which one won.")
    st.dataframe(
        hourly_decision.sort_values("ts_utc")[
            [
                "ts_berlin",
                "price_eur_per_mwh",
                "temperature_c",
                "total_demand_mwth",
                "gas_total_cost_eur",
                "electric_total_cost_eur",
                "smart_switching_cost_eur",
                "chosen_source",
            ]
        ],
        width="stretch",
        height=560,
        column_config={
            "ts_berlin": st.column_config.DatetimeColumn("Time (Berlin)", format="D MMM YYYY, HH:mm"),
            "price_eur_per_mwh": st.column_config.NumberColumn("Price (EUR/MWh)", format="%.2f"),
            "temperature_c": st.column_config.NumberColumn("Temp (°C)", format="%.1f"),
            "total_demand_mwth": st.column_config.NumberColumn("Demand (MWth)", format="%.2f"),
            "gas_total_cost_eur": st.column_config.NumberColumn("Gas cost (EUR)", format="%.2f"),
            "electric_total_cost_eur": st.column_config.NumberColumn("Electric cost (EUR)", format="%.2f"),
            "smart_switching_cost_eur": st.column_config.NumberColumn("Cost paid (EUR)", format="%.2f"),
            "chosen_source": st.column_config.TextColumn("Chosen"),
        },
    )

with tab_forecast:
    st.info(
        "**Context only.** The savings and payback figures above use ACTUAL historical prices, not "
        "a forecast -- this section only checks whether a simple forward-looking forecast would "
        "have been good enough to act on, and is not an input to any number shown elsewhere on this page."
    )
    st.subheader("Seasonal-naive vs. persistence")
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
    winner = (
        "Persistence" if row["persistence_mae_eur_per_mwh"] < row["seasonal_naive_mae_eur_per_mwh"]
        else "Seasonal-naive"
    )
    st.success(f"**{winner}** wins on MAE over the {int(row['backtest_hours'])}-hour backtest window.")

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
    st.caption("Last 2 weeks of the backtest window shown; the metrics above cover the full window.")

with tab_assumptions:
    st.subheader("Every number this model runs on")
    st.caption(
        "Market data (price, weather) is real and independently verified against both live APIs. "
        "Everything below is a stated assumption -- confidence reflects how directly it's sourced."
    )

    CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
    CONFIDENCE_LABEL = {
        "high": "High -- first-principles or direct citation",
        "medium": "Medium -- cited external source",
        "low": "Low -- placeholder, needs real data",
    }
    # (background, text) pairs -- both set explicitly so the row stays readable regardless of
    # whether Streamlit is rendering in light or dark theme (a background alone left the default
    # theme text color in place, which was unreadable against these light tints in dark mode)
    CONFIDENCE_STYLE = {
        "high": ("#DEEBE0", "#1F4A2C"),
        "medium": ("#FBF3D9", "#6B5900"),
        "low": ("#F3DEDC", "#7A2E28"),
    }

    def format_value(v):
        if isinstance(v, float):
            return f"{v:g}"
        return str(v)

    assumption_rows = []
    for key, entry in assumptions.items():
        if not isinstance(entry, dict):
            continue
        if key == "capex_eur_per_mwth":
            # this entry has 3 values (low/mid/high), not one -- show the full range instead
            # of silently picking "mid" and dropping the other two
            value = f"{entry['low']:,.0f} / {entry['mid']:,.0f} / {entry['high']:,.0f} (low/mid/high)"
        else:
            value = format_value(entry.get("value", entry))
        confidence = str(entry.get("confidence", "")).lower()
        assumption_rows.append(
            {
                "field": key,
                "value": value,
                "confidence": CONFIDENCE_LABEL.get(confidence, confidence or "--"),
                "source": entry.get("source", ""),
                "_confidence_raw": confidence,
                "_sort": CONFIDENCE_ORDER.get(confidence, 9),
            }
        )
    assumption_df = pd.DataFrame(assumption_rows).sort_values("_sort")

    def highlight_confidence(row):
        bg, fg = CONFIDENCE_STYLE.get(row["_confidence_raw"], ("", ""))
        style = f"background-color: {bg}; color: {fg}" if bg else ""
        return [style] * len(row)

    display_df = assumption_df[["field", "value", "confidence", "source", "_confidence_raw"]]
    st.dataframe(
        display_df.style.apply(highlight_confidence, axis=1).hide(axis="columns", subset=["_confidence_raw"]),
        width="stretch",
        hide_index=True,
        column_config={
            "field": st.column_config.TextColumn("Field", width="medium"),
            "value": st.column_config.TextColumn("Value", width="medium"),
            "confidence": st.column_config.TextColumn("Confidence", width="medium"),
            "source": st.column_config.TextColumn("Source", width="large"),
        },
    )
    st.caption("🟩 high confidence   🟨 medium confidence   🟥 low confidence, needs real data before this is a business case")
