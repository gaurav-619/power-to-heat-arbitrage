-- Day-ahead price series, tz-aware.
-- ts_utc:    the absolute instant (TIMESTAMP WITH TIME ZONE) -- use this to join to other
--            series, since it is unambiguous even on DST transition days.
-- ts_berlin: the same instant expressed as Europe/Berlin wall-clock time (naive), for
--            display/grouping only. On the fall-back day this value repeats for two
--            different ts_utc instants (the ambiguous local hour), so it must never be
--            used as a join key.
select
    to_timestamp(timestamp_utc_ms / 1000.0) as ts_utc,
    to_timestamp(timestamp_utc_ms / 1000.0) at time zone 'Europe/Berlin' as ts_berlin,
    price_eur_per_mwh
from read_csv_auto('{{ var("data_dir") }}/prices.csv')
