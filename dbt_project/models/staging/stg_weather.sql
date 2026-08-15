-- Hourly temperature series, tz-aware. The source CSV timestamps are UTC-naive (Open-Meteo
-- was called with no `timezone` param, which defaults to GMT/UTC, offset zero -- verified
-- against a live response and asserted at fetch time in ingestion/fetch_weather.py).
-- read_csv_auto already infers timestamp_utc_naive as a naive TIMESTAMP.
-- See stg_prices.sql for why ts_utc (not ts_berlin) is the correct join key.
select
    timestamp_utc_naive at time zone 'UTC' as ts_utc,
    (timestamp_utc_naive at time zone 'UTC') at time zone 'Europe/Berlin' as ts_berlin,
    temperature_c
from read_csv_auto('{{ var("data_dir") }}/weather.csv')
