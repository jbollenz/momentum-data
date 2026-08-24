# momentum-data

Automated market-data collector for a personal equity-research momentum
dashboard. A scheduled GitHub Action runs on weekday evenings after the NYSE
close, downloads prices from Yahoo Finance (via `yfinance`), macro series
from FRED and three public sentiment sources, **computes all derived metrics
server-side**, and commits the results as JSON files into `data/`. A separate
dashboard reads those files over
`https://raw.githubusercontent.com/<user>/momentum-data/main/data/<file>.json`.

For first-time setup through the GitHub web interface, see [SETUP.md](SETUP.md).

**Schema version 2.** `data/meta.json` carries `"schema_version": 2`. A
dashboard should refuse to render on an unknown major version rather than
guess at field names.

## Design rule

Every number in the pack is **computed or parsed programmatically inside the
Action**. Nothing is left to be read off a web page by a human or a language
model later. That is why `derived.json`, `breadth.json` and `market.json`
exist: moving averages, 52-week highs, relative volume, breadth percentages
and the third-party sentiment numbers are all calculated here, from full
daily history, and shipped as plain numbers.

## How it works

- `universe.yaml` is the single source of truth for what gets collected: US
  and European ETFs, European indices, volatility indices, FX, ~90 European
  large caps, the S&P 500 (resolved automatically at run time), and a list of
  FRED series. Edit it and commit; the next run picks up the change.
- `collector.py` does the downloading, the maths and the parsing, and writes
  the eight JSON files described below. Every network call is wrapped: a
  failing ticker, series or source never aborts the run, it is recorded in
  `data/meta.json`.
- `.github/workflows/collect.yml` runs the collector at **22:20 UTC** and
  again at **01:20 UTC** the next morning (a second attempt in case the first
  cron is delayed or dropped — GitHub's scheduler is best-effort). The second
  attempt skips itself when the first one already produced fresh data. It can
  also be started by hand at any time: **Actions → Collect market data → Run
  workflow** (a manual run never skips).

The workflow shows red when the collector exits non-zero — but it commits
`data/meta.json` first, so the dashboard can always tell "the run failed"
(`run_ok: false`) from "the run never happened" (old `run_utc`). Partial
problems show up as a green run with details in `data/meta.json`.

## Two downloads, one basis

| pass | tickers | request | shipped as |
|------|---------|---------|-----------|
| ETF / index / vol / FX | ~82 | 3 years daily OHLCV | `etf_daily.json` (full) + `etf_weekly.json` (resampled) |
| stocks (US + EU) | ~590 | 2 years daily OHLCV | `stocks_daily.json` (last 130 sessions only) |

Both passes use `auto_adjust=True`. The full 2-year stock history is used
in-Action for `derived.json` and `breadth.json` but is **not** shipped, to
keep the files small. The weekly series is resampled from the same daily
bars, so weekly and daily can never disagree.

## Conventions shared by all files

- Dates are strings, `YYYY-MM-DD`, ascending. Within one series the `dates`,
  `close`, `high`, `low`, `volume` / `values` arrays are always the same
  length.
- **Adjustment policy:** closes are adjusted for **splits and dividends**
  (`auto_adjust=True`, total-return basis). `meta.json` states this verbatim
  in its `adjustment` field. The whole series is re-downloaded and rewritten
  every run, so a new dividend restates history consistently.
- Prices are rounded to 4 decimals and are in each instrument's **local
  trading currency** (no FX conversion anywhere).
- Volumes are integers (0 where the venue reports none, e.g. indices, FX).
- Missing days are simply absent — nothing is forward-filled, gaps are
  preserved as reported by the source.
- A metric is `null` when the history is too short for its definition. The
  collector never extrapolates, pads or guesses.
- Every file has `"as_of"`: the last date present for the majority of its
  series (`null` when the file is empty).
- **Partial-bar guard:** if the last bar's date equals the current session
  date for that ticker's market and that market has not closed yet at run
  time, the bar is dropped. A delayed cron or a mid-session manual run can
  therefore never commit an in-progress bar as final. Tickers affected are
  listed in `meta.json` → `partial_bar_guard_dropped`.

Markets are assigned per ticker: **EU** for Yahoo suffixes
`.DE .AS .PA .MI .MC .BR .HE .LS .CO .ST .OL .VI .IR` and for the European
index symbols in `universe.yaml`; **US** for everything else (including
`^VIX`, `^VIX3M` and `EURUSD=X`).

---

## `meta.json` — run report and freshness contract

```json
{
  "schema_version": 2,
  "run_utc": "2026-08-24T22:24:11Z",
  "run_ok": true,
  "as_of": "2026-08-24",
  "adjustment": "auto_adjust=True — closes adjusted for splits AND dividends (total-return basis)",
  "yfinance_version": "1.6.0",
  "pandas_version": "3.0.2",
  "numpy_version": "2.4.4",
  "expected_last_trading_day": {"US": "2026-08-24", "EU": "2026-08-24"},
  "freshness": {
    "US": {"expected": "2026-08-24", "actual": "2026-08-24", "lag_sessions": 0},
    "EU": {"expected": "2026-08-24", "actual": "2026-08-24", "lag_sessions": 0}
  },
  "last_bar_dates": {"SPY": "2026-08-24", "SAP.DE": "2026-08-24"},
  "stale_tickers": ["XYZ"],
  "partial_bar_guard_dropped": [],
  "counts": {"etfs_us": {"requested": 50, "ok": 50, "failed": 0, "dropped": 0}},
  "failed_tickers": {"stocks_us": ["XYZ"]},
  "dropped_tickers": {},
  "market_sources": {"cnn_fear_greed": "ok", "cboe_putcall": "ok",
                     "insider_cluster_buys": "failed: HTTPError: 403 ..."},
  "files": ["meta.json", "etf_daily.json", "..."],
  "notes": []
}
```

Freshness fields:

- `expected_last_trading_day` — per market, the most recent session whose
  close had already passed at run time. Computed from a built-in calendar:
  weekends, hardcoded NYSE and Xetra holiday lists for **2026 and 2027**, US
  DST (2nd Sunday March → 1st Sunday November) and EU DST (last Sunday March
  → last Sunday October), with closes at NYSE 20:00 UTC summer / 21:00 UTC
  winter and Xetra 15:30 / 16:30 UTC. Xetra is the reference calendar for all
  of Europe; it closes latest, so a Euronext or Nordic bar is never mistaken
  for an in-progress one.
- `freshness.<market>.actual` — the **modal** last-bar date across that
  market's tickers.
- `freshness.<market>.lag_sessions` — trading sessions between `actual` and
  `expected` (0 = fully current). `null` when the market has no data at all.
  Suggested dashboard ladder: 0 = green, 1 = amber, >1 = red banner with
  "today/current" language suppressed.
- `last_bar_dates` — last shipped bar date for **every** series in the pack.
- `stale_tickers` — tickers more than **3 trading sessions** behind their
  market's expected last trading day.
- `run_ok` — `false` when the collector failed (no price data at all, or a
  crash). When it crashed, `error` holds the exception and the other files in
  `data/` are left over from the previous successful run.
- `counts` per bucket is measured on the bucket's primary shipped file and
  always satisfies `ok + failed + dropped = requested`. `failed_tickers` is
  the union of failures across all downloads.

Calendar caveat: the holiday tables cover 2026–2027. From 2028 the collector
falls back to weekend-only logic and says so in `notes` — refresh
`US_HOLIDAYS` / `EU_HOLIDAYS` in `collector.py` before then. Nordic and
Euronext exchange-specific holidays are not modelled; on those days a handful
of tickers sit one session behind, well inside the 3-session stale threshold.

---

## `etf_daily.json` — 3 years of daily OHLCV for the ETF/index universe

Covers `etfs_us`, `etfs_eu`, `indices_eu`, `vol` and `fx` from
`universe.yaml`. `high` / `low` are the raw session extremes (used for true
52-week highs); if the venue reports no high/low they fall back to the close.

```json
{
  "as_of": "2026-08-24",
  "series": {
    "SPY": {
      "dates":  ["2023-08-25", "..."],
      "close":  [560.1234, "..."],
      "high":   [562.8000, "..."],
      "low":    [558.4100, "..."],
      "volume": [45882300, "..."]
    }
  }
}
```

## `etf_weekly.json` — 3 years of weekly closes, resampled from the daily bars

Same tickers as `etf_daily.json`. Weekly = `W-FRI` last close, i.e. the last
available daily close of each Monday-to-Friday week. This is what the RRG /
relative-rotation maths runs on.

- `dates` are the **Friday that ends each week**, even when the week's last
  trading day was a Thursday.
- The final label can therefore be a date in the **future**: the current week
  is still running. `last_daily_date` (= `as_of`) is the last daily close
  actually used, so the ambiguity is always resolvable.

```json
{
  "as_of": "2026-08-24",
  "last_daily_date": "2026-08-24",
  "series": {
    "SPY": {"dates": ["2023-08-25", "..."], "close": [560.1234, "..."]}
  }
}
```

## `stocks_daily.json` — last 130 trading days, all stocks

US (S&P 500, resolved at run time) and EU stocks merged, for sparklines and
volume display. `region` maps every ticker present in `series` to `"US"` or
`"EU"`. The full 2-year history behind these tickers is used for
`derived.json` and `breadth.json` but is not shipped. Tickers with fewer than
150 daily closes are dropped from the whole pack (listed under
`dropped_tickers` in `meta.json`).

```json
{
  "as_of": "2026-08-24",
  "region": {"AAPL": "US", "SAP.DE": "EU"},
  "series": {
    "AAPL": {"dates": ["..."], "close": [225.4321, "..."], "volume": [51234500, "..."]}
  }
}
```

## `derived.json` — per-ticker metrics, computed server-side

Every ticker in the pack (ETFs, indices, vol, FX **and** stocks), computed
from that ticker's full daily history in memory (3 years for the ETF
universe, 2 years for stocks). All floats rounded to 4 decimals. A field is
`null` when the history is shorter than the window it needs.

```json
{
  "as_of": "2026-08-24",
  "metrics": {
    "SPY": {"last_close": 560.1234, "ret_1m": 3.522, "sma200": 512.44, "...": "..."}
  }
}
```

Lookbacks are **trading days**, not calendar days. All returns and distances
are **percentages** (e.g. `3.522` means +3.522%).

| field | definition | needs |
|---|---|---|
| `last_close` | last adjusted close | 1 bar |
| `last_date` | date of that close, `YYYY-MM-DD` | 1 bar |
| `ret_1w` | simple total return over 5 sessions: `close[-1]/close[-6]-1` | 6 |
| `ret_1m` | same over 21 sessions | 22 |
| `ret_3m` | same over 63 sessions | 64 |
| `ret_6m` | same over 126 sessions | 127 |
| `ret_12m` | same over 252 sessions | 253 |
| `mom_12_1` | return from t−252 to t−21: `close[-22]/close[-253]-1` (skips the most recent month) | 253 |
| `sma50` | mean of the last 50 closes | 50 |
| `sma200` | mean of the last 200 closes | 200 |
| `pct_vs_sma50` | `last_close/sma50-1`, percent above (+) or below (−) | 50 |
| `pct_vs_sma200` | `last_close/sma200-1` | 200 |
| `sma200_slope_20d` | percent change of the 200-day SMA over the last 20 sessions | 220 |
| `high_52w` | highest daily **High** of the last 252 sessions | 252 |
| `low_52w` | lowest daily **Low** of the last 252 sessions | 252 |
| `pct_from_52w_high` | `last_close/high_52w-1` — **negative** below the high | 252 |
| `pct_above_52w_low` | `last_close/low_52w-1` — positive above the low | 252 |
| `adv20` | 20-day average **dollar** volume: `mean(close*volume)` over 20 sessions, in the local currency | 20 |
| `rvol` | relative volume: last session's volume ÷ mean volume of the **20 sessions before it** (the last session is excluded from the average). `null` if that average is 0 | 21 |
| `vol_20d_ann` | annualised stdev of daily log returns over the last 20 returns: `std(ddof=1) × √252 × 100`, in percent | 21 |
| `vol_60d_ann` | same over the last 60 returns | 61 |
| `atr14_pct` | mean of the last 14 daily true ranges ÷ `last_close` × 100, where `TR = max(high−low, |high−prev_close|, |low−prev_close|)`. Simple mean, not Wilder smoothing. Needs High/Low | 15 |
| `drawdown_from_252d_high` | `last_close / max(close over the last 252 sessions) − 1` — **close-based**, so it differs slightly from `pct_from_52w_high`, which uses intraday highs | 252 |
| `above_sma200` | boolean, `last_close > sma200` | 200 |
| `above_sma50` | boolean, `last_close > sma50` | 50 |
| `new_52w_high_20d` | boolean: on any of the last 20 sessions, that session's High equalled the highest High of its trailing 252 sessions | 271 |

If a ticker's High/Low column contains a NaN anywhere inside the window a
metric needs, that metric falls back to closes (`high_52w`, `low_52w`,
`new_52w_high_20d`) or becomes `null` (`atr14_pct`) — it is never computed
from a silently shortened window.

## `breadth.json` — participation history per region

Computed in-Action from the full 2-year stock panel, for the last **250
sessions** of each region, US and EU separately.

```json
{
  "as_of": "2026-08-24",
  "US": {
    "dates": ["2025-08-26", "..."],
    "pct_above_sma200": [62.7291, "..."],
    "pct_above_sma50": [55.1938, "..."],
    "net_new_highs_20d": [1.0183, "..."],
    "n_eligible": [491, "..."]
  },
  "EU": {"...": "..."}
}
```

- **Eligibility** (one definition, used as the denominator for all three
  series): on that date the ticker has an actual close **and** at least 200
  prior sessions of its own history. `n_eligible` is that count — always ship
  it on screen, or at least flag thin coverage.
- `pct_above_sma200` / `pct_above_sma50` — percent of eligible tickers whose
  close on that date is above their own 200- / 50-day SMA on that date.
- `net_new_highs_20d` — (count whose close is the highest of its trailing 20
  sessions) − (count whose close is the lowest), as a percent of eligible.
  Close-based, not intraday. A perfectly flat series counts as both and
  contributes 0.
- A region's session list is the union of its tickers' dates, filtered to
  dates where at least half the region actually traded — so one exchange's
  odd session does not create a phantom breadth point. On days when part of
  Europe is on holiday `n_eligible` drops; that is real, not a bug.
- All three series are `null` on a date where `n_eligible` is 0. Leading
  dates with no eligible ticker at all are omitted.

## `market.json` — parsed third-party sources (best-effort)

Three independent sources, each fetched once with a 30-second timeout. Any of
them can fail without affecting the rest of the run: the entry then carries
`"ok": false` and an `error` string, and `meta.json` → `market_sources`
records it. **Always check `ok` before reading a source.**

```json
{
  "as_of": "2026-08-24",
  "sources": {
    "cnn_fear_greed": {
      "ok": true, "fetched_utc": "2026-08-24T22:23:02Z",
      "score": 45.34, "rating": "fear", "timestamp": "2026-08-21T23:59:56+00:00",
      "component_keys": ["junk_bond_demand", "market_momentum_sp125", "..."],
      "components": {"junk_bond_demand": {"score": 70.3, "rating": "greed",
                                          "timestamp": "2026-08-21"}},
      "historical": {"dates": ["..."], "values": [45.34, "..."]}
    },
    "cboe_putcall": {
      "ok": true, "fetched_utc": "...",
      "ratios": {"TOTAL PUT/CALL RATIO": 0.92, "EQUITY PUT/CALL RATIO": 0.63},
      "labels": ["EQUITY PUT/CALL RATIO", "..."],
      "report_date_raw": "August 21, 2026",
      "parsed_with": ["pandas.read_html"]
    },
    "insider_cluster_buys": {
      "ok": true, "fetched_utc": "...",
      "source": "openinsider.com (third-party aggregation of SEC Form 4)",
      "row_count": 60, "columns_missing": [],
      "rows": [{"filing_date": "2026-08-21 18:31:07", "trade_date": "2026-08-19",
                "ticker": "ACME", "company": "Acme Corp", "insiders_count": 3,
                "trade_type": "P - Purchase", "price": 28.06, "qty": 52500,
                "owned": 1234567, "delta_own": "+13%",
                "value_usd": 1473069, "value_raw": "+$1,473,069"}]
    }
  }
}
```

**`cnn_fear_greed`** — `https://production.dataviz.cnn.io/index/fearandgreed/graphdata`.
`score` / `rating` / `timestamp` are the composite. `components` holds each
sub-index's current score and rating, and `component_keys` lists exactly
which keys the payload contained on this run — the collector reads them off
the response rather than assuming a fixed set, so a CNN rename shows up as a
changed `component_keys` list instead of missing data. `historical` is the
last 250 points of `fear_and_greed_historical`.

**`cboe_putcall`** — `https://www.cboe.com/us/options/market_statistics/daily/`.
`ratios` maps every `<something> PUT/CALL RATIO` label found on the page to
its number. **No expected set is hardcoded**: read `labels` to see what was
actually there this run, and treat a missing label as missing, not as zero.
The parser tries `pandas.read_html` first and a regex over the stripped text
as a fallback (`parsed_with` says which worked). If the page shape changes so
that nothing is recognised, the source is recorded as failed rather than
guessed at. `report_date_raw` is the date string found on the page, verbatim
and unparsed.

**`insider_cluster_buys`** — `http://openinsider.com/latest-cluster-buys`,
first 60 rows. **This is a third-party aggregation of SEC Form 4 filings, not
an SEC feed** — the `source` field says so explicitly and any UI showing
these rows should repeat it. It is unaudited, skews small- and micro-cap, and
mixes transaction codes and 10%-owner purchases. Do not join it into a
leaders screen or present it as verified insider activity. `value_usd` is the
parsed integer (`"+$1,473,069"` → `1473069`); `value_raw` keeps the original
string so the parse can be checked. `columns_missing` lists any normalised
field the page did not provide.

Because CNN and CBOE reject non-browser clients, these three requests send a
browser-style `User-Agent`; everything else (Yahoo, FRED, the constituents
CSV) uses the collector's own identifying agent string.

## `fred.json` — 8 years of FRED macro series

Series IDs come from `fred_series` in `universe.yaml`; the depth is 8 years so
that rolling percentile baselines are meaningful. FRED's `.` markers (missing
observations) are dropped. Current list: `VIXCLS` (VIX close),
`BAMLH0A0HYM2` (US high-yield OAS), `T10Y2Y` (10y−2y spread), `DGS10`,
`DGS2`, `DTWEXBGS` (broad trade-weighted dollar), `BAMLC0A0CM` (US
investment-grade OAS), `NFCI` (Chicago Fed financial conditions, weekly).

```json
{
  "as_of": "2026-08-21",
  "series": {"VIXCLS": {"dates": ["..."], "values": [15.5, "..."]}}
}
```

## `sp500_constituents.csv`

Cached copy of the S&P 500 member list (from the public
[`datasets/s-and-p-500-companies`](https://github.com/datasets/s-and-p-500-companies)
dataset), refreshed on every successful run and used as a fallback when the
download fails. Symbols are converted to Yahoo notation (`BRK.B` → `BRK-B`).

---

## Running manually

From the GitHub website: **Actions tab → Collect market data → Run
workflow → Run workflow** (green button). A manual run always collects, even
if today's data already looks fresh. The run takes a few minutes; when it
finishes, `data/` has a fresh commit.

Locally (optional, needs Python 3.12):

```bash
pip install -r requirements.txt
python collector.py
python tests/test_collector.py     # unit tests, no network needed
```

## Keeping the schedule alive

GitHub automatically disables *scheduled* workflows in repositories that have
shown no activity for about 60 days. The data commits this workflow makes
normally count as activity — and because the commit step runs even when
collection fails (the `meta.json` heartbeat), the schedule survives a broken
collector too. If GitHub ever emails you that the scheduled workflow was
disabled, open the **Actions** tab, select **Collect market data**, and click
**Enable workflow**.

Daily commits make the git history grow steadily (roughly 6 MB of JSON per
run). That is harmless for years of operation; if the repository ever feels
bloated, the simplest fix is to recreate it from the current files.

## Data sources and disclaimer

Prices come from Yahoo Finance's public endpoints via the open-source
`yfinance` library; macro series from FRED's public CSV endpoint; sentiment
from CNN's public dataviz endpoint, CBOE's daily statistics page, and
openinsider.com. None of these is a contractual data feed: symbols can
change, values are unaudited, pages get restructured, and access can break
without notice — which is why every source failure is recorded in
`meta.json` instead of being papered over. Entries marked `verify: true` in
`universe.yaml` have a best-effort label that has not been hand-checked. This
repository is for **personal research and educational use only** — nothing
here is investment advice, and the data should not be redistributed
commercially.
