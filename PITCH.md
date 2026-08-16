# The Electrification Case — How It Was Built

This is the full walkthrough: exactly where every number came from and exactly how it was calculated, traced with real values from the actual pipeline run — not illustrative round numbers. If you want the short pitch-ready version instead, the summary tables are in the second half; if you want to understand *why* a number is what it is, read from the top.

> **Data vintage:** this run covers **14 Aug 2025 – 14 Aug 2026** (8,784 hours), pulled 16 Aug 2026. Every figure below traces back to that specific run. Re-running the pipeline on a later date shifts the 12-month window forward and every number moves slightly — see [Reproducing this analysis](#reproducing-this-analysis).

---

## 1. Where the data comes from

Two things had to be fetched for every hour of the year: what electricity cost, and how cold it was. Both come from public APIs, no key required, no manual entry anywhere.

### 1.1 Electricity prices — SMARD (Bundesnetzagentur)

SMARD publishes Germany's day-ahead electricity price in two steps:

1. **An index call** tells you which weekly chunks of data exist:
   `GET https://www.smard.de/app/chart_data/4169/DE-LU/index_hour.json`
   → returns `{"timestamps": [1538344800000, 1538949600000, ...]}` — one entry per ~7-day chunk, each a UTC millisecond epoch marking where that chunk starts.

2. **A chunk call** for each timestamp you need fetches the actual prices:
   `GET https://www.smard.de/app/chart_data/4169/DE-LU/4169_DE-LU_hour_<chunk_ts>.json`
   → returns `{"meta_data": {...}, "series": [[1785708000000, 139.79], [1785711600000, 135.11], ...]}` — up to 168 `[timestamp_ms, price_eur_per_mwh]` pairs.

`ingestion/fetch_prices.py` calls the index once, works out which chunks overlap the target date range, fetches each one, and keeps only the rows inside the exact window. Every chunk's raw response is saved untouched to `data/raw/prices_<chunk_ts>.json` before anything is filtered — so the original API response is always on disk to check against.

I didn't guess this shape — the docs page at `smard.api.bund.dev` renders no static schema (it's a JS app that returns nothing to a plain fetch), so before writing the script I hit both endpoints directly and read the real response first. That's also why the script hard-asserts the response has a `"series"` key and prints the actual keys found if it doesn't, rather than silently misreading a changed API.

Filter `4169` is the DE-LU day-ahead price. Since 1 Oct 2025 the actual EPEX auction clears at 15-minute resolution, but SMARD keeps publishing this same hourly index/series pair straight through that date, so the hourly series was used for the whole year without a resolution change partway through.

### 1.2 Weather — Open-Meteo

One call, no chunking needed:

`GET https://archive-api.open-meteo.com/v1/archive?latitude=49.48&longitude=8.45&start_date=...&end_date=...&hourly=temperature_2m`

→ returns `{"hourly": {"time": ["2026-08-01T00:00", ...], "temperature_2m": [22.5, ...]}, "hourly_units": {"temperature_2m": "°C"}, "timezone": "GMT", "utc_offset_seconds": 0, ...}`.

Latitude/longitude 49.48, 8.45 is Ludwigshafen — chosen as a stand-in for "a generic German industrial site," not because any real Celsio facility is there.

**A real bug lived here, and this is exactly how it was caught.** Open-Meteo interprets `start_date`/`end_date` as GMT calendar days — confirmed by reading `"timezone": "GMT"` in a live response — while `fetch_prices.py` bounds its date range in *Europe/Berlin* calendar days, to match. Berlin is UTC+1 or +2 depending on the season, so requesting the "same" date range from both APIs actually asked Open-Meteo for a window shifted by 1–2 hours. Left alone, that silently clipped a couple of hours off one end of the weather series relative to the price series. It surfaced as two rows disappearing on the join between the two staging tables — 8,782 rows instead of the expected 8,784 — which is what test-driven the fix: pad the Open-Meteo request by a day on each side, then trim the result back down to the exact Europe/Berlin-aligned bounds before writing `data/processed/weather.csv`. After the fix, both series are 8,784 rows and the join is exact.

---

## 2. Turning raw JSON into one clean hourly table

Both fetch scripts do the same three things before anything is trusted:

1. **Assert the shape.** If the JSON doesn't have the expected keys, the script prints the actual keys it found and exits — it does not try to guess a compatible structure.
2. **Assert the unit.** For weather, the script checks `hourly_units.temperature_2m == "°C"` from the live response rather than assuming it (SMARD doesn't return a unit field at all, so that one is documented in the script as a hard-coded fact from SMARD's own docs, with a plausibility range check as a backstop — €/MWh values are asserted to fall within [−1000, 5000], well outside which would indicate something is wrong with the parse, not the market).
3. **Assert the range.** Every value is checked against a sane physical bound (temperature within [−40, 50]°C) before being written out.

The two resulting CSVs (`data/processed/prices.csv`, `data/processed/weather.csv`) are then loaded by dbt (`dbt_project/models/staging/`) and given two timestamp columns each:

- **`ts_utc`** — the absolute instant, timezone-aware. This is the join key.
- **`ts_berlin`** — the same instant expressed as Europe/Berlin wall-clock time, for display and for grouping by calendar day.

The join key matters more than it looks: on the one day a year the clocks fall back (26 Oct 2025), the hour between 02:00 and 03:00 happens *twice* in Berlin local time — so `ts_berlin` briefly repeats. Joining the two series on that column would match every price row for that hour against every weather row for that hour, producing duplicate, scrambled pairs for one hour a year. Joining on `ts_utc` instead — the real, non-repeating instant — sidesteps that entirely; `ts_berlin` is kept only as a label.

---

## 3. One hour, start to finish

Everything above is abstract until you watch one real hour move through it. Take **Thursday 15 January 2026, 08:00 Berlin time** — an unremarkable winter morning, chosen precisely because nothing dramatic happens in it.

**Step 1 — the raw API bytes.** That instant is 2026-01-15 07:00:00 UTC (Berlin was on CET, UTC+1, in January), which is epoch millisecond `1768460400000`. Searching the saved raw files for that exact number finds it inside `data/raw/prices_1768172400000.json` (a chunk that starts 2026-01-11 23:00 UTC, so our hour is 80 hours into that 168-hour chunk):

```
[1768456800000, 105.12], [1768460400000, 104.98], [1768464000000, 101.41]
```

Our hour's raw price: **104.98 EUR/MWh.** The matching entry in `data/raw/weather.json`:

```
"2026-01-15T06:00" → 7.2,  "2026-01-15T07:00" → 7.4,  "2026-01-15T08:00" → 6.9
```

Our hour's raw temperature: **7.4°C.**

**Step 2 — demand.** The synthetic plant's heat demand only rises when it's colder than 15°C:

```
extra_demand_mwth = max(15.0 - 7.4, 0) * 0.05 = 7.6 * 0.05 = 0.38
total_demand_mwth = 4.0 + 0.38 = 4.38 MWth
```

**Step 3 — cost of each option.**

```
gas_cost_per_mwh      = 38.21 / 0.88            = 43.42 EUR/MWh
electric_cost_per_mwh = (104.98 + 24.85) / 0.99 = 131.14 EUR/MWh

gas_total_cost_eur      = 43.42 * 4.38  = 190.18
electric_total_cost_eur = 131.14 * 4.38 = 574.40
```

*(The two per-MWh rates are shown rounded to 2 decimal places for readability; the pipeline itself carries full floating-point precision — 43.420454545... and 131.141414... — through the multiplication. Multiplying the rounded rates shown above by hand lands on €574.39 for the electric total, one cent off €574.40; that cent is rounding display noise, not a different number.)*

**Step 4 — decide.** 190.18 < 574.40, so this hour is a **gas** hour, and the "smart switching" strategy pays the gas price: €190.18. That's a fairly typical outcome — a moderately cold morning with an unremarkable price rarely makes grid power competitive once the €24.85 markup and the 99%-vs-88% efficiency gap are both accounted for.

This is a genuinely traceable example, not a constructed one — every number above is the literal value stored in the pipeline's DuckDB output for this exact hour, back-verified against the exact bytes the two APIs returned for it.

---

## 4. From one hour to a year

There's no separate "annual calculation" — the annual and monthly figures are just sums of the per-hour numbers from step 3 above, repeated 8,784 times (once per real hour in the dataset) and aggregated two ways:

- **"Always gas"** sums every hour's `gas_total_cost_eur`, regardless of what electricity would have cost.
- **"Smart switching"** sums, for every hour, whichever of `gas_total_cost_eur` / `electric_total_cost_eur` is smaller.

Grouping those same per-hour rows by calendar month instead of by year gives the monthly table in [What we found](#what-we-found) below — same numbers, same formula, just summed over 28–31 hours-a-day instead of 8,784.

The two are guaranteed never to cross — smart-switching cost is a per-hour minimum of the same two numbers "always gas" uses one of, so its sum can never exceed the gas-only sum. `tests/test_decision_logic.py` checks this holds on all 366 individual days anyway (not just the annual total), specifically so a future change to the cost formula can't silently break the guarantee without a test failing.

---

## 5. How the price forecast was computed

A forecast is only useful if you'd know it *before* the hour happens, so both methods only ever look backward from the point of the forecast — never at the actual future value they're trying to predict.

**Seasonal-naive**, continuing the same 15 Jan 2026 08:00 hour as an example: this is a Thursday, so the forecast is the average of the actual price at 08:00 on the **4 preceding Thursdays**:

| Date | Thursday 08:00 price |
|---|---:|
| 8 Jan 2026 | €168.81 |
| 1 Jan 2026 | €0.06 |
| 25 Dec 2025 | €64.21 |
| 18 Dec 2025 | €90.74 |

```
forecast = (168.81 + 0.06 + 64.21 + 90.74) / 4 = 323.82 / 4 = 80.955 EUR/MWh
actual   = 104.98 EUR/MWh
error    = |104.98 - 80.955| = 24.03 EUR/MWh
```

Notice what happened here: two of the four "prior Thursdays" were **New Year's Day and Christmas Day** — both unusually low-demand, low-price holidays that don't resemble a normal Thursday at all. The seasonal-naive method has no concept of a holiday; it just averages the last 4 same-weekday-same-hour prices, so those two outliers drag its forecast down and it underestimates by €24.03.

**Persistence**, same hour: just the actual price at 08:00 the previous calendar day (Wednesday 14 Jan 2026, an ordinary day):

```
forecast = €128.96
actual   = €104.98
error    = |104.98 - 128.96| = 23.98 EUR/MWh
```

Both methods miss by a similar amount on this particular hour, but persistence edges it out — and that pattern held up across the full backtest (see [Forecasting the price](#forecasting-the-price)), because persistence never has a holiday-contamination problem: it only ever looks at yesterday, which is far more likely to resemble today than "the same weekday 1–4 weeks ago" is, especially around any month with a public holiday in it.

The backtest that produces the headline MAE/MAPE figures runs this exact same pair of calculations for every hour in the dataset, skips the first 4 weeks (there aren't yet 4 prior same-weekday-hour occurrences to average for the seasonal-naive method), and takes the mean absolute error across everything that's left — 8,111 hours.

---

## 6. How the payback table was computed

Take the "mid CAPEX, base gas price" cell as a worked example — the middle of the 3×3 grid, arguably the most-likely-case number:

```
capex_total_eur  = capex_eur_per_mwth[mid] * plant_base_load_mwth
                 = 137,500 * 4.0
                 = 550,000 EUR

annual_savings_eur (base gas price) = 97,626.19 EUR   <- from section 4, "always gas" minus "smart switching", summed over the full year

payback_years = capex_total_eur / annual_savings_eur
              = 550,000 / 97,626.19
              = 5.63 years
```

The other 8 cells repeat the exact same division, varying only which of the two inputs changes:

- **CAPEX** varies by row (low/mid/high = €125k / €137.5k / €150k per MWth) — changes `capex_total_eur` only.
- **Gas price** varies by column (base ±20%) — this one is subtler, because changing the gas price doesn't just rescale the payback number, it changes `annual_savings_eur` itself. A higher gas price makes "always gas" more expensive across all 8,784 hours (electric cost is untouched, since it depends on the day-ahead price, not the gas price), which *widens* the gap smart-switching captures — so the entire section-3/section-4 calculation is re-run three times, once per gas-price scenario, each producing its own `annual_savings_eur`, before any division by CAPEX happens.

That's why the answer is a 9-cell table and not a single number scaled 9 ways.

---

## 7. How this was checked

Nothing above was taken on faith — each piece has a test that would fail if it stopped being true:

- **The formulas themselves** (`tests/test_decision_logic.py`) — 4 hand-picked (price, temperature) pairs, 2 where gas should win and 2 where electric should win, with the expected cost computed independently in the test file itself (not imported from the pipeline code) and asserted to match exactly.
- **The never-lose guarantee** — the same test file queries the real pipeline output and asserts `smart_switching_cost ≤ always_gas_cost` on every one of the 366 individual days, not just the annual total.
- **Timezone/DST correctness** (`tests/test_timezone_join.py`) — builds a synthetic hourly UTC grid across the two real DST transition dates in this dataset's window (26 Oct 2025 fall-back, 29 Mar 2026 spring-forward) using the exact same DuckDB `AT TIME ZONE` mechanism the staging models use, and asserts the fall-back day has 25 local hours and the spring-forward day has 23 — then separately re-checks that the *actual* pipeline output has the same 25/23 split on those two real dates.
- **Unit arithmetic** (`tests/test_units.py`) — confirms `power (MW) × 1 hour = energy (MWh)` numerically, and that every consecutive pair of rows in the real output is exactly 60 minutes apart (so no row silently represents anything other than one hour).

All 13 tests pass against the live 8,784-row dataset as of this run.

---

## What we found

*(Reference tables — see sections 1–7 above for how each of these numbers was actually produced.)*

| | | | |
|---|---|---|---|
| **€97,626** | **3.7–9.5 yrs** | **11.7%** | **13/13** |
| annual savings, smart switching vs. always-gas (6.0%) | CAPEX payback range, 9 scenarios | of hours electricity was the cheaper option | tests passing · 10/10 build checks passing |

| Strategy | Annual cost |
|---|---:|
| Always gas | €1,621,531 |
| Smart switching | €1,523,905 |
| **Savings** | **€97,626 · 6.0%** |

**By month** — savings are concentrated, not evenly spread. April and May 2026 account for nearly half the year's total, driven by a handful of extreme negative-price hours (the single lowest, 1 May 2026 13:00, cleared at −€499/MWh — the plant would have been *paid* €1,916 to run the electric heater that hour against a €174 gas cost). Winter months, where demand is high and gas usually wins, contribute almost nothing:

| Month | Always gas | Smart switching | Savings |
|---|---:|---:|---:|
| 2025-08 | €75,229 | €71,744 | €3,484 |
| 2025-09 | €126,873 | €117,229 | €9,644 |
| 2025-10 | €135,251 | €127,551 | €7,700 |
| 2025-11 | €139,471 | €139,122 | €349 |
| 2025-12 | €147,151 | €146,697 | €453 |
| 2026-01 | €151,467 | €149,771 | €1,696 |
| 2026-02 | €130,069 | €129,297 | €772 |
| 2026-03 | €140,582 | €134,512 | €6,069 |
| **2026-04** | €130,509 | €101,500 | **€29,009** |
| **2026-05** | €131,927 | €113,829 | **€18,099** |
| 2026-06 | €125,399 | €117,832 | €7,568 |
| 2026-07 | €129,247 | €120,015 | €9,233 |
| 2026-08 (partial) | €58,357 | €54,806 | €3,551 |

*(Monthly figures are each independently rounded to the nearest euro, so they sum to €97,627 — €1 off the precisely-computed annual total of €97,626. That's rounding, not a discrepancy in the underlying data.)*

### Forecasting the price

| Method | Definition | MAE (€/MWh) | MAPE* |
|---|---|---:|---:|
| Seasonal-naive | Mean of the same Europe/Berlin weekday+hour, preceding 4 occurrences | 31.42 | 138.1% |
| **Persistence** | Same hour, previous calendar day | **28.10** | **96.4%** |

Persistence wins on both metrics, for the reason worked through in section 5 above — see that section for why, not just that.

\* MAPE computed only over 7,805 of the 8,111 backtest hours (|price| ≥ €1/MWh) — the excluded 306 hours (3.8% of the backtest window) are close enough to zero that a percentage-error metric explodes or is undefined. MAE (which has no such issue) is the primary number; treat MAPE as secondary.

### The financial case

| CAPEX \ Gas price | −20% (€30.57) | Base (€38.21) | +20% (€45.85) |
|---|---:|---:|---:|
| **Low** (€125k/MWth) | 7.9 yrs | 5.1 yrs | 3.7 yrs |
| **Mid** (€137.5k/MWth) | 8.7 yrs | 5.6 yrs | 4.0 yrs |
| **High** (€150k/MWth) | 9.5 yrs | 6.1 yrs | 4.4 yrs |

Range spans **3.7 to 9.5 years** across all nine scenarios — quote the range in a pitch, never a single point number.

### Assumptions

Every non-market number the model runs on, with where it came from. **VERIFIED / high** means load-bearing arithmetic from a cited source; **ASSUMED / low** means a placeholder that needs real data before this is a real business case.

| Field | Value | Status | Source |
|---|---:|---|---|
| Plant base load | 4.0 MWth | ASSUMED · low | Sized within the 3–5 MWth range where CAPEX sources converge |
| Reference temperature | 15.0°C | ASSUMED · low | Simplified heating-degree threshold, not a real thermodynamic model |
| Temp sensitivity | 0.05 MWth/°C | ASSUMED · low | Kept small to avoid overstating weather sensitivity |
| Gas price | €38.21/MWh | VERIFIED · medium | Midpoint of EEX Natural Gas Year Futures 2026 avg (36.42) & ECCO Climate 2025 projection (40): (36.42+40)/2 = 38.21 |
| Gas boiler efficiency | 88% | VERIFIED · medium | Typical modern industrial gas boiler, 85–92% range |
| Electric heater efficiency | 99% | VERIFIED · high | Thermodynamic first principles — resistive heating, near-zero conversion loss |
| Electricity markup | €24.85/MWh | VERIFIED · medium | Sum of DE industrial levies: tax 0.5 + offshore 6.6 + KWK 2.75 + network (midpoint of 11–19) 15 = 24.85 |
| CAPEX (low/mid/high) | 125,000 / 137,500 / 150,000 €/MWth | VERIFIED · medium | McKinsey 2024 EU electrification analysis (low case), cross-checked vs. a 2018-USD-basis US DOE model; mid = arithmetic midpoint of low/high |

---

## Known limitations

- **No carbon price.** Neither EU ETS nor any carbon levy is in either cost formula. Adding one would push the case further toward electric, not away from it — so this makes the savings figure conservative, not optimistic.
- **Gas price is a year-ahead futures average, not current spot.** €38.21/MWh is cited as the midpoint of an EEX Natural Gas Year Futures 2026 average and an ECCO Climate 2025 projection — checked against live market data, German wholesale gas hit €62.54/MWh on 14 Aug 2026 (TTF ~€60.56/MWh the same week), above even the +20% scenario (€45.85) already in the payback table. Partly an acute August supply shock (Qatar LNG halt after an attack), not necessarily lasting — but real as of this writing. Same direction as the carbon-price gap: a higher gas price only makes gas more expensive, so this makes the case conservative, not optimistic.
- **CAPEX isn't Germany- or 2026-specific.** Best available public figures, not a vendor quote. Treat the payback range as directional.
- **One consumer class throughout.** Gas price, markup, and CAPEX all assume the same large-industrial baseload consumer, kept consistent for an apples-to-apples comparison.
- **Binary hourly decision.** Each hour is 100% gas or 100% electric for the plant's whole demand — no partial load-shifting.
- **Single weather station.** Ludwigshafen is an assumed proxy, not a real facility.

## Before you pitch this

**Solid enough to state as-is:** the market data is real and independently verified against both live APIs; the decision arithmetic is hand-checked and test-covered; timezone/DST handling is correct and proven; the methodology (retrospective actuals + backtested forecast + sensitivity table) is sound.

**Needed before this is Celsio's business case:** there is no real plant behind the demand model yet — base load, temperature sensitivity, and reference temperature are all invented placeholders, and this is the single biggest gap; no vendor CAPEX quotes exist yet; only one year of history has been checked, not a volatile year like 2022; and the gas price assumption is a year-ahead futures average running well below the current spot price observed in Aug 2026 (see Known Limitations) — worth deciding deliberately whether a futures/contract basis or a spot basis is the right comparison for how Celsio actually buys gas, rather than leaving it as an unstated choice. The GitHub Action still hasn't run on real infrastructure (no scheduled trigger has fired yet), though the dashboard itself is no longer localhost-only.

---

## Reproducing this analysis

```bash
# from the project root
python ingestion/fetch_prices.py
python ingestion/fetch_weather.py
cd dbt_project && dbt run --profiles-dir .
cd .. && pytest tests/ -v
streamlit run dashboard/app.py
```

*celsio-prototype · built with SMARD (Bundesnetzagentur), Open-Meteo, dbt-duckdb, Streamlit · data vintage 16 Aug 2026*
