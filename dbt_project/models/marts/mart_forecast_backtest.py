"""Seasonal-naive vs. persistence price forecasting, backtested over the ingested year.

Seasonal-naive: for a given (Europe/Berlin weekday, hour-of-day), forecast = mean of the
preceding 4 occurrences of that same (weekday, hour) actual price.

Persistence baseline: forecast = actual price at the same Berlin hour-of-day, one calendar
day earlier.

Both are computed on Europe/Berlin wall-clock (weekday, hour, date) fields rather than a
fixed 168/24-row shift, so they hold correctly across the DST transition weeks (a fixed
row-shift would be off by an hour across the boundary; a wall-clock lookup simply returns
no match for an hour that did not occur, e.g. 02:00 on the spring-forward day).

Backtest window: excludes the first 4 weeks of the dataset (section 6) since the
seasonal-naive method needs 4 prior occurrences before its first real forecast.

MAPE is undefined/explosive for near-zero or negative day-ahead prices (which happen in
Germany); rows with |actual| < EUR 1/MWh are excluded from the MAPE calculation only (not
from MAE, and not from the returned per-hour table) -- see README Known Limitations.
"""

import numpy as np
import pandas as pd

WEEKLY_LOOKBACK = 4
BACKTEST_EXCLUDE_DAYS = 28
MAPE_MIN_ABS_PRICE = 1.0


def model(dbt, session):
    dbt.config(materialized="table")

    prices = dbt.ref("stg_prices").df().sort_values("ts_berlin").reset_index(drop=True)
    prices["weekday"] = prices["ts_berlin"].dt.weekday
    prices["hour_of_day"] = prices["ts_berlin"].dt.hour
    prices["berlin_date"] = prices["ts_berlin"].dt.date

    # seasonal-naive: mean of the preceding WEEKLY_LOOKBACK occurrences of the same
    # (weekday, hour_of_day), ordered by actual calendar date within each group
    grouped = prices.groupby(["weekday", "hour_of_day"])["price_eur_per_mwh"]
    prices["seasonal_naive_forecast"] = grouped.transform(
        lambda s: s.shift(1).rolling(window=WEEKLY_LOOKBACK, min_periods=WEEKLY_LOOKBACK).mean()
    )

    # persistence: same Berlin hour-of-day, previous calendar day (wall-clock lookup, so a
    # nonexistent local hour on a DST day correctly yields no forecast rather than a bogus one)
    lookup = prices.set_index(["berlin_date", "hour_of_day"])["price_eur_per_mwh"].to_dict()
    prev_dates = prices["berlin_date"] - pd.Timedelta(days=1)
    prices["persistence_forecast"] = [
        lookup.get((d, h)) for d, h in zip(prev_dates, prices["hour_of_day"])
    ]

    backtest_start = prices["berlin_date"].min() + pd.Timedelta(days=BACKTEST_EXCLUDE_DAYS)
    prices["in_backtest_window"] = prices["berlin_date"] >= backtest_start

    for col in ["seasonal_naive_forecast", "persistence_forecast"]:
        err_col = col.replace("_forecast", "_abs_error")
        prices[err_col] = (prices["price_eur_per_mwh"] - prices[col]).abs()

    return prices[
        [
            "ts_utc",
            "ts_berlin",
            "berlin_date",
            "weekday",
            "hour_of_day",
            "price_eur_per_mwh",
            "seasonal_naive_forecast",
            "seasonal_naive_abs_error",
            "persistence_forecast",
            "persistence_abs_error",
            "in_backtest_window",
        ]
    ]
