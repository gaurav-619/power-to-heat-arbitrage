-- Annual cost comparison over the full ingested year, using ACTUAL historical day-ahead
-- prices (retrospective "what would we have paid" analysis -- see mart_forecast_backtest
-- for the forward-looking forecast quality assessment instead).
with daily as (
    select
        cast(ts_berlin as date) as berlin_date,
        sum(gas_total_cost_eur) as daily_always_gas_cost_eur,
        sum(smart_switching_cost_eur) as daily_smart_switching_cost_eur
    from {{ ref('int_hourly_decision') }}
    group by 1
)
select
    berlin_date,
    daily_always_gas_cost_eur,
    daily_smart_switching_cost_eur,
    daily_always_gas_cost_eur - daily_smart_switching_cost_eur as daily_savings_eur,
    sum(daily_always_gas_cost_eur) over () as annual_always_gas_cost_eur,
    sum(daily_smart_switching_cost_eur) over () as annual_smart_switching_cost_eur,
    sum(daily_always_gas_cost_eur - daily_smart_switching_cost_eur) over () as annual_savings_eur
from daily
order by berlin_date
