-- MAE/MAPE for both forecasting methods over the backtest window (section 6).
-- MAPE excludes hours with |actual price| < EUR 1/MWh (near-zero/negative prices make MAPE
-- explode/undefined) -- MAE uses every backtest hour, no exclusion.
with bt as (
    select *
    from {{ ref('mart_forecast_backtest') }}
    where in_backtest_window
      and seasonal_naive_forecast is not null
      and persistence_forecast is not null
),
mape_rows as (
    select *
    from bt
    where abs(price_eur_per_mwh) >= 1.0
)
select
    (select count(*) from bt) as backtest_hours,
    (select count(*) from mape_rows) as mape_eligible_hours,
    (select avg(seasonal_naive_abs_error) from bt) as seasonal_naive_mae_eur_per_mwh,
    (select avg(persistence_abs_error) from bt) as persistence_mae_eur_per_mwh,
    (select avg(seasonal_naive_abs_error / abs(price_eur_per_mwh)) * 100 from mape_rows) as seasonal_naive_mape_pct,
    (select avg(persistence_abs_error / abs(price_eur_per_mwh)) * 100 from mape_rows) as persistence_mape_pct
