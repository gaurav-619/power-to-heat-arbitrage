# How the Code Works

`PITCH.md` explains the data and the results. This one explains the actual code — every file, what each function does, and why it's written the way it is. Read this if you want to be able to open any file in the project and already know what you're looking at.

## The shape of the project

Five kinds of files, each with one job:

```
ingestion/fetch_prices.py       ─┐
ingestion/fetch_weather.py      ─┴─→ pure Python, no dbt involved. Talk to the two APIs,
                                      write two CSVs. Nothing downstream can run without these.

dbt_project/models/staging/     ─── SQL. Load the CSVs, add timezone-aware timestamp columns.

dbt_project/models/intermediate/── Python. Join the two staging tables, run the actual
                                    gas-vs-electric formula, hour by hour.

dbt_project/models/marts/       ─── Mix of SQL and Python. Roll the hourly table up into
                                     the things a person actually wants to look at: annual
                                     costs, a forecast backtest, a payback table.

dashboard/app.py                ─── Streamlit. Reads the finished DuckDB file, read-only,
                                     and draws it on screen. Never computes anything itself.

tests/                          ─── pytest. Independently re-checks the formulas, the
                                     timezone handling, and the unit conversion.
```

dbt figures out the run order itself. Every model file that depends on another one says so explicitly — a SQL model writes `{{ ref('other_model') }}`, a Python model writes `dbt.ref('other_model')` — and dbt reads all of those references across all 7 model files to build a dependency graph, then runs them in the only order that makes sense (both staging models first, since neither depends on anything; then the intermediate model, since it needs both staging tables; then the marts, since they need the intermediate table or each other). Nobody hard-coded that order anywhere — it falls out of who calls `ref()` on whom.

---

## `ingestion/fetch_prices.py`

**Job:** turn the SMARD API into `data/processed/prices.csv`.

A handful of module-level constants at the top (`BASE_URL`, `FILTER_ID = "4169"`, `REGION = "DE-LU"`, ...) exist so the URL is built in one place instead of typed out fresh in three different functions. `PLAUSIBLE_MIN, PLAUSIBLE_MAX = -1000.0, 5000.0` is a sanity fence — German prices really do go negative, so the lower bound is generous, but a value outside this range almost certainly means the parse went wrong somewhere, not that the market did something new.

- **`resolve_date_range()`** — `end_date = today - 2 days`, `start_date = end_date - 365 days`. Prints them, returns them. That's the entire "most recent 12 months" rule; there's no other place in the codebase that decides the date range.

- **`berlin_bounds_utc_ms(start_date, end_date)`** — takes those two calendar dates and turns them into a precise UTC millisecond window. It builds a Python `datetime` for *midnight on start_date, in Berlin time*, and another for *midnight the day after end_date, in Berlin time* (i.e., the exact end of end_date) — then converts both to UTC and to milliseconds. The reason it goes through Berlin midnight rather than just using the dates directly: Berlin is UTC+1 or +2 depending on the season, so "midnight in Berlin" is not the same UTC instant in August as it is in January, and this function is what gets that conversion right automatically regardless of which season the range happens to fall in.

- **`fetch_json(url)`** — one-line wrapper: GET, raise on HTTP error, return the parsed JSON. Every other function funnels through this so there's one place that handles the network call.

- **`fetch_index()` / `fetch_chunk(chunk_ts)`** — call the two SMARD endpoints described in `PITCH.md` section 1.1. Both check that the JSON they got back actually has the key they expect (`"timestamps"` or `"series"`) before touching it, and if it doesn't, they print every key that *was* there and call `sys.exit(1)` — deliberately not trying to guess a different shape or silently return an empty result. If SMARD ever changes its response format, this fails loudly the next run instead of quietly writing wrong numbers.

- **`main()`** does, in order:
  1. Resolve the date range, convert to UTC ms bounds.
  2. Fetch the index (SMARD's full history — currently a few hundred weekly chunk timestamps) and keep only the chunks whose 7-day span overlaps our window (`ts + CHUNK_SPAN_MS > start_ms and ts < end_ms`).
  3. For each needed chunk: fetch it, write the *untouched* response to `data/raw/prices_<chunk_ts>.json` — before any filtering happens, so the raw API bytes are always recoverable later — then walk its `[timestamp, value]` pairs, discard anything outside the exact window, and for everything else assert it's not null, is a number, and is within the plausible range.
  4. Collect surviving rows into a plain dict keyed by timestamp (so if two chunks ever overlapped on a timestamp, the second write would just overwrite the first rather than creating a duplicate row), sort by timestamp, and write `prices.csv`.

---

## `ingestion/fetch_weather.py`

**Job:** turn the Open-Meteo API into `data/processed/weather.csv`. Same overall shape as `fetch_prices.py`, with one extra wrinkle that's worth understanding because it's where the real bug was.

- **`resolve_date_range()`** — identical logic to the price script.

- **`berlin_bounds_utc(start_date, end_date)`** — same idea as `berlin_bounds_utc_ms`, but returns actual `datetime` objects instead of integer milliseconds, because this script compares them against parsed ISO timestamp strings rather than an epoch integer.

- **`main()`**:
  1. Compute the *precise* Europe/Berlin-aligned UTC bounds (`start_utc`, `end_utc`) — this is the range we actually want.
  2. Compute a *padded* range, `±1 day` wider, and send **that** to Open-Meteo (`fetch_start_date`, `fetch_end_date`).
  3. Save the raw response to `data/raw/weather.json`.
  4. Validate the response shape (`hourly`, `hourly_units` keys present), the unit string (`"°C"` exactly), and — this is the important one — that the response's own `timezone` field says `"GMT"` and `utc_offset_seconds` is `0`. That third check is a live guard on the exact assumption the whole script depends on: if Open-Meteo ever changed its default behavior, this would fail loudly instead of silently mis-timezoning every row.
  5. Loop over the returned `(time_string, temperature)` pairs. Parse each time string into a real UTC-aware `datetime`, then **only keep it if it falls inside the precise, unpadded window** from step 1 — this is where the extra day fetched in step 2 gets trimmed back off.

Why the padding exists at all: Open-Meteo interprets `start_date`/`end_date` as **GMT calendar days**, not Europe/Berlin calendar days. If the script asked for exactly the Berlin-aligned dates, it would silently get a window shifted by 1–2 hours relative to the price series — which is exactly what happened the first time this was built, and showed up as 2 missing rows on the join between the two staging tables. Padding the request and trimming the result in code, rather than trusting the API to interpret the dates the way we want, is the fix.

---

## `dbt_project/models/staging/` — two small SQL files

**`stg_prices.sql`** reads the CSV with DuckDB's `read_csv_auto(...)`, which inspects the file and infers column types on its own (no schema declared by hand). The one interesting line:

```sql
select
    to_timestamp(timestamp_utc_ms / 1000.0) as ts_utc,
    to_timestamp(timestamp_utc_ms / 1000.0) at time zone 'Europe/Berlin' as ts_berlin,
    price_eur_per_mwh
from read_csv_auto('{{ var("data_dir") }}/prices.csv')
```

`to_timestamp(seconds)` turns the millisecond column into a proper timezone-aware instant — that's `ts_utc`. Appending `at time zone 'Europe/Berlin'` to that same expression asks DuckDB "what would this instant read as on a clock in Berlin" — that's `ts_berlin`, a plain (timezone-less) reading used only for display and for grouping rows by calendar day.

`{{ var("data_dir") }}` is dbt's templating syntax — it's replaced at run time with whatever `data_dir` is set to in `dbt_project.yml` (`../data/processed`), so the path lives in exactly one place instead of being retyped in every model that reads a CSV.

**`stg_weather.sql`** does the same conversion in the opposite direction, because its source column is already a parsed timestamp with no timezone attached, not raw milliseconds:

```sql
select
    timestamp_utc_naive at time zone 'UTC' as ts_utc,
    (timestamp_utc_naive at time zone 'UTC') at time zone 'Europe/Berlin' as ts_berlin,
    temperature_c
from read_csv_auto('{{ var("data_dir") }}/weather.csv')
```

`timestamp_utc_naive at time zone 'UTC'` here means "treat this timezone-less reading as if it were a UTC clock" — which is exactly correct, because `fetch_weather.py` already confirmed live that Open-Meteo returns GMT/UTC. That produces `ts_utc`; converting it again to Berlin produces `ts_berlin`, same as the price side.

Both staging models are configured as dbt **views**, not tables (see `dbt_project.yml`) — meaning DuckDB re-runs this small conversion from the CSV every time something downstream queries it, rather than storing a copy. That's a deliberate choice: the conversion is cheap, so there's no benefit to persisting it, and a view can never go stale relative to the CSV underneath it.

---

## `dbt_project/models/intermediate/int_hourly_decision.py` — the core formula

This one is a **dbt Python model**, not SQL — a plain `.py` file with a function called `model(dbt, session)`. dbt-duckdb runs that function directly (DuckDB is embedded in the same process, so there's no separate Spark cluster or anything like that involved) and takes whatever it returns as the new table's contents.

- **`load_assumptions()`** opens `config/assumptions.yaml` fresh on every single run — nothing from that file is ever hard-coded into this script — and checks whether any entry's `source` text starts with `"NOT FOUND"`, printing a warning if so. None of the 8 real entries currently trigger this; it exists so that if someone later adds an unverifiable assumption, the pipeline complains at run time instead of quietly using it.

- **`model(dbt, session)`**, step by step:

  ```python
  dbt.config(materialized="table")
  prices = dbt.ref("stg_prices").df()
  weather = dbt.ref("stg_weather").df()
  df = prices.merge(weather[["ts_utc", "temperature_c"]], on="ts_utc", how="inner")
  ```

  `dbt.ref("stg_prices")` is how this model tells dbt "I depend on stg_prices" — that's the mechanism the whole-project dependency graph is built from. `.df()` pulls the referenced table into a pandas DataFrame. `.merge(..., on="ts_utc")` is a standard inner join: keep a row only if that exact `ts_utc` instant exists in both tables. Joining on `ts_utc` (not `ts_berlin`) is deliberate — see the staging section above for why the Berlin label is unsafe as a join key.

  ```python
  extra_demand_mwth = (reference_temp_c - df["temperature_c"]).clip(lower=0) * temp_sensitivity
  df["total_demand_mwth"] = base_load_mwth + extra_demand_mwth
  ```

  This is the "if colder than reference, add demand, otherwise zero" rule from `PITCH.md`, written without an `if`. Subtracting temperature from the reference gives a positive number on a cold hour and a negative number on a warm hour; `.clip(lower=0)` clamps every negative value up to exactly zero, which *is* the "otherwise zero" branch. It runs on the whole column at once (this is what "vectorized" means in pandas) rather than looping row by row — same result, much faster, and it's why there's no `for` loop anywhere in this file even though it's computing something for every one of 8,784 rows.

  ```python
  df["gas_cost_per_mwh"] = gas_price / gas_efficiency
  df["electric_cost_per_mwh"] = (df["price_eur_per_mwh"] + markup) / electric_efficiency
  df["gas_total_cost_eur"] = df["gas_cost_per_mwh"] * df["total_demand_mwth"]
  df["electric_total_cost_eur"] = df["electric_cost_per_mwh"] * df["total_demand_mwth"]
  ```

  `gas_cost_per_mwh` is a single constant broadcast onto every row — it never changes hour to hour, since the gas price is assumed fixed and only the electric side depends on the (constantly changing) day-ahead price.

  ```python
  df["smart_switching_cost_eur"] = np.minimum(df["gas_total_cost_eur"], df["electric_total_cost_eur"])
  df["chosen_source"] = np.where(df["electric_total_cost_eur"] < df["gas_total_cost_eur"], "electric", "gas")
  ```

  `np.minimum` takes the smaller of the two cost columns, row by row. `np.where(condition, a, b)` reads as "wherever condition is true, put a; otherwise put b" — again applied to the whole column in one call.

The function ends by returning a DataFrame with just the useful columns — dbt-duckdb writes whatever comes back as the `int_hourly_decision` table.

---

## `dbt_project/models/marts/` — four files, four different jobs

**`mart_annual_costs.sql`** — pure SQL. A `with daily as (...)` block first collapses the 8,784 hourly rows down to 366 daily rows (`group by cast(ts_berlin as date)`, summing each day's gas cost and smart-switching cost). The outer query then adds four more columns using `sum(...) over ()` — a *window function*, which computes a total across every row in the result **without** collapsing them the way a normal `group by` would. That's how a single table ends up with both the day-by-day numbers (for the trend chart) and the one grand annual total (for the headline stat) sitting side by side in every row, from one query.

**`mart_forecast_backtest.py`** — another Python model, and the most involved piece of code in the project.

```python
prices = dbt.ref("stg_prices").df().sort_values("ts_berlin").reset_index(drop=True)
prices["weekday"] = prices["ts_berlin"].dt.weekday
prices["hour_of_day"] = prices["ts_berlin"].dt.hour
prices["berlin_date"] = prices["ts_berlin"].dt.date
```

Three label columns get added: which day of the week (0=Monday), which hour of the day (0–23), and the plain calendar date — these are what the two forecasting methods group and look up by.

```python
grouped = prices.groupby(["weekday", "hour_of_day"])["price_eur_per_mwh"]
prices["seasonal_naive_forecast"] = grouped.transform(
    lambda s: s.shift(1).rolling(window=4, min_periods=4).mean()
)
```

`.groupby(["weekday", "hour_of_day"])` buckets rows together — every Thursday-08:00 row across the whole year lands in the same bucket, in chronological order (because the whole table was sorted by time first). Within each bucket: `.shift(1)` moves every value down one position, so a row can never see its own actual price, only earlier ones; `.rolling(window=4, min_periods=4).mean()` then averages the 4 values immediately before the current position. `min_periods=4` means "don't produce an answer at all until 4 prior values genuinely exist" — that's why the first 4 weeks of the dataset have no seasonal-naive forecast, and why the backtest excludes them. `.transform(...)` (as opposed to `.agg(...)`) puts the result back in the original row order rather than collapsing each group to one row.

```python
lookup = prices.set_index(["berlin_date", "hour_of_day"])["price_eur_per_mwh"].to_dict()
prev_dates = prices["berlin_date"] - pd.Timedelta(days=1)
prices["persistence_forecast"] = [lookup.get((d, h)) for d, h in zip(prev_dates, prices["hour_of_day"])]
```

The persistence forecast is built differently — a plain dictionary keyed by `(date, hour)` for instant lookup, then for every row it looks up "yesterday, same hour." Using `.get(...)` (which returns `None` if the key isn't found, rather than crashing) matters on the two DST days: if a given local hour genuinely didn't occur the day before, the lookup just quietly returns no forecast instead of guessing something wrong.

```python
backtest_start = prices["berlin_date"].min() + pd.Timedelta(days=28)
prices["in_backtest_window"] = prices["berlin_date"] >= backtest_start
```

Flags every row from day 29 onward as eligible for the backtest — the first 28 days are the seasonal-naive method's mandatory warm-up.

**`mart_forecast_summary.sql`** — SQL again, and purely an aggregation on top of the table the previous model produced. `with bt as (...)` filters to backtest-window rows that have both forecasts present; `mape_rows` filters `bt` further, to rows where `abs(price_eur_per_mwh) >= 1.0` (the near-zero-price exclusion). The final `select` uses six correlated subqueries — `(select ... from bt)` — each computing one independent number (row counts, both MAEs, both MAPEs) into a single summary row.

**`mart_payback_table.py`** — reads `int_hourly_decision` (not the raw CSVs — it specifically needs `total_demand_mwth` and `electric_total_cost_eur`, which only exist after that model has already run) and loads the assumptions file again, independently — dbt Python models don't share state with each other, so every model that needs a constant re-reads it fresh.

```python
for gas_label, gas_multiplier in GAS_PRICE_SCENARIOS:      # 3 iterations
    scenario_gas_price = base_gas_price * gas_multiplier
    gas_total_cost = (scenario_gas_price / gas_efficiency) * demand
    annual_savings = gas_total_cost.sum() - np.minimum(gas_total_cost, electric_total_cost).sum()

    for capex_label, capex_per_mwth in capex_scenarios:      # 3 iterations each = 9 total
        payback_years = (capex_per_mwth * base_load_mwth) / annual_savings
        rows.append({...})
```

Two nested loops, 3×3 = 9 rows. The outer loop re-derives the *entire year's* gas cost and savings figure from scratch for each of the 3 gas-price scenarios (electric cost is reused unchanged, since it never depended on the gas price to begin with) — that's the part that's easy to miss just looking at the final table: it isn't one savings number scaled 9 ways, it's genuinely 3 different savings numbers, each then divided by 3 different CAPEX totals.

---

## `tests/` — three files, each checking a different kind of thing

**`test_decision_logic.py`** starts by re-implementing the formula from scratch, in `compute_hour()` — deliberately *not* importing it from `int_hourly_decision.py`. If someone later breaks the real pipeline's formula, this independent copy is what notices, rather than the test trivially agreeing with whatever the code happens to do. Four test functions each hard-code one `(price, temperature)` pair with the expected result shown in a comment above it — 2 built to make gas win, 2 built to make electric win. A fifth test is different in kind: instead of inventing inputs, it opens the real DuckDB file (if it exists — `@pytest.mark.skipif` skips this test rather than failing it when the pipeline hasn't been run yet) and checks the never-lose guarantee against real output, across all 366 real days.

**`test_timezone_join.py`**'s core trick is `berlin_local_hour_count()`: it builds a tiny synthetic table directly in DuckDB — `range(?)` generates the integers `0..N-1`, and `?::timestamptz + interval (i) hour` turns each one into "the start instant plus i hours," a perfectly regular clock with no gaps — then counts how many of those instants land on a given calendar date once converted to Europe/Berlin. That's the exact same `AT TIME ZONE` mechanism the real staging models use, exercised on a small made-up dataset instead of the real one, so this test needs no network access and is fully deterministic. Three tests use it (the known fall-back date should count 25, the known spring-forward date should count 23, an ordinary date should count 24, as a control), and a fourth re-checks the same 25/23 split against the actual pipeline output.

**`test_units.py`** checks the simplest thing in the project — that `power × 1 hour = energy`, numerically — because that identity is the entire justification for why the rest of the codebase multiplies an hourly demand figure directly by a per-MWh price with no separate "×1 hour" step written anywhere. Its last test queries the real output and checks, using DuckDB's `lag()` window function, that every consecutive pair of rows really is exactly 60 minutes apart — if the data ever had a gap or a duplicate hour, that assumption (and every cost total built on it) would be silently wrong, and this is what would catch it.

---

## `dashboard/app.py`

Reads the finished DuckDB file and displays it — it never computes any of the numbers itself, only pulls already-finished tables and formats them.

- `@st.cache_resource` on the DuckDB connection and `@st.cache_data` on the table loaders exist because Streamlit re-runs the *entire script* top to bottom on every user interaction (clicking a chart, resizing a panel); without caching, that would reopen the database file and re-run every query on every click.
- If the `.duckdb` file doesn't exist yet, it shows the three commands needed to produce it and calls `st.stop()` — deliberately not letting the rest of the script run into a crash.
- The 4 headline KPI numbers are computed directly from the `annual_costs` table right there in the dashboard code (`annual_always_gas.sum()`, etc.) — not read from some separately pre-computed constant, so they can never drift out of sync with what's actually in the database.
- The payback table gets `.pivot(...)` reshaped from 9 rows into a 3×3 grid, then `.reindex(...)` forces the row/column order to low→mid→high and −20%→base→+20% — without that, a plain pivot sorts alphabetically, which would put "+20%" before "−20%" before "base."
- The forecast chart only plots the last 14 days of the ~8,000-row backtest (`.tail(24*14)`) — the full backtest would be an unreadable smear as a line chart, so it's deliberately sliced to a legible window.

---

## `dbt_project.yml` / `profiles.yml`

`dbt_project.yml`'s `vars:` block (`data_dir`, `config_dir`) is why the CSV path only appears once, centrally, instead of being retyped in every SQL model — every model reads it via `{{ var("data_dir") }}`. Its `models:` block sets staging to materialize as lightweight **views** (cheap, always fresh) and intermediate/marts as real **tables** (persisted, since they involve actual computation worth not repeating on every query).

`profiles.yml` tells dbt which engine to use (`type: duckdb`) and where the `.duckdb` file lives on disk, plus one setting — `TimeZone: "UTC"` — that pins DuckDB's own session timezone, so the date-conversion behavior in the staging models doesn't silently depend on whatever timezone the machine running it happens to be set to.

---

## Running it end to end

```bash
python ingestion/fetch_prices.py     # no dbt involved yet — just the two CSVs
python ingestion/fetch_weather.py
cd dbt_project && dbt run --profiles-dir .   # dbt reads all 7 model files, works out
                                              # the dependency order from ref() calls,
                                              # and runs them: staging → intermediate → marts
cd .. && pytest tests/ -v            # re-checks the formulas, DST handling, and units
streamlit run dashboard/app.py       # opens the finished .duckdb file, read-only
```

Nothing in this sequence is optional or reorderable except the two ingestion scripts (their order relative to each other doesn't matter) — everything after `dbt run` depends on the DuckDB file that step produces.
