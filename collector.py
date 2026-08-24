#!/usr/bin/env python3
"""Daily market-data collector for the momentum dashboard.

Reads universe.yaml, downloads prices from Yahoo Finance (via yfinance),
macro series from FRED and three extra public sources (CNN Fear & Greed,
CBOE daily put/call statistics, openinsider cluster buys), then writes
JSON snapshots into data/.

Design rule for this collector: *every number the dashboard shows must be
computed or parsed here, programmatically*. Nothing is left for a
downstream language model to transcribe. That is why the pack ships
`derived.json` (per-ticker momentum / trend / volatility metrics),
`breadth.json` (participation history) and `market.json` (parsed
third-party sentiment sources) alongside the raw price series.

The script runs unattended inside GitHub Actions, so every network step is
defensive: a failing ticker, batch, series or source is logged, recorded in
data/meta.json, and the run carries on. The process exits non-zero only
when no price data could be downloaded at all (or on an unexpected crash),
and in both cases a meta.json heartbeat is still written so the dashboard
can tell "run failed" from "run never happened".

The eight output files and their schema are documented in README.md.
"""

import html as html_module
import io
import json
import logging
import re
import sys
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml
import yfinance as yf

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UNIVERSE_FILE = ROOT / "universe.yaml"

SCHEMA_VERSION = 2
ADJUSTMENT_NOTE = ("auto_adjust=True — closes adjusted for splits AND dividends "
                   "(total-return basis)")

SP500_URL = ("https://raw.githubusercontent.com/datasets/"
             "s-and-p-500-companies/main/data/constituents.csv")
SP500_CACHE = DATA_DIR / "sp500_constituents.csv"
SP500_MIN_ROWS = 400          # sanity floor: the real list has ~503 symbols
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_YEARS = 8                # percentile baselines need depth

CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CBOE_PUTCALL_URL = "https://www.cboe.com/us/options/market_statistics/daily/"
OPENINSIDER_URL = "http://openinsider.com/latest-cluster-buys"
OPENINSIDER_SOURCE_TAG = "openinsider.com (third-party aggregation of SEC Form 4)"
OPENINSIDER_MAX_ROWS = 60

USER_AGENT = "Mozilla/5.0 (compatible; momentum-data-collector/2.0)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 30          # seconds, for plain requests calls
BATCH_SIZE = 100              # tickers per yf.download call
BATCH_PAUSE = 2.0             # seconds between batches
RETRY_PAUSE = 20.0            # seconds before retrying a failed batch

ETF_PERIOD = "3y"             # ETF/index/vol/FX daily depth
STOCK_PERIOD = "2y"           # stock daily depth (one pass, used for derived+breadth)
STOCKS_KEEP_POINTS = 130      # trading days actually shipped in stocks_daily.json
MIN_DAILY_POINTS = 150        # stocks with less daily history than this are dropped
BREADTH_SESSIONS = 250        # sessions shipped in breadth.json
CNN_HISTORY_POINTS = 250      # points shipped from the CNN historical series
STALE_LAG_SESSIONS = 3        # a ticker this many sessions behind is "stale"
SIZE_WARN_MB = 8.0

# Yahoo suffixes that identify a European (Xetra-ish) trading calendar.
EU_SUFFIXES = (".DE", ".AS", ".PA", ".MI", ".MC", ".BR", ".HE",
               ".LS", ".CO", ".ST", ".OL", ".VI", ".IR")

log = logging.getLogger("collector")


# ============================================================ generic helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def http_get(url: str, params: dict | None = None, *, browser: bool = False):
    """GET a URL once, politely. Raises on HTTP errors."""
    headers = {"User-Agent": BROWSER_UA if browser else USER_AGENT,
               "Accept-Language": "en-US,en;q=0.9"}
    if browser:
        headers["Accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                             "application/json;q=0.9,*/*;q=0.8")
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                            headers=headers)
    response.raise_for_status()
    return response


def http_get_text(url: str, params: dict | None = None, *, browser: bool = False) -> str:
    return http_get(url, params, browser=browser).text


def write_json(path: Path, payload: dict) -> None:
    """Write compact JSON with sorted keys so daily git diffs stay minimal.

    Strict on purpose: `allow_nan=False` and no `default=` coercion, so a NaN
    or a stray numpy scalar raises here instead of silently reaching the
    dashboard as `NaN` or as a quoted string.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")
    size_mb = path.stat().st_size / 1_000_000
    log.info("wrote %s (%.2f MB)", path.name, size_mb)
    if size_mb > SIZE_WARN_MB:
        log.warning("%s is larger than the %.0f MB target", path.name, SIZE_WARN_MB)


def r4(value) -> float | None:
    """Round to 4 dp, mapping NaN/inf/None to None (never guess a number)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return round(number, 4)


def pct_of(numerator, denominator) -> float | None:
    """(numerator / denominator - 1) * 100, or None when undefined."""
    if numerator is None or denominator is None:
        return None
    try:
        num, den = float(numerator), float(denominator)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(num) and np.isfinite(den)) or den == 0:
        return None
    return round((num / den - 1.0) * 100.0, 4)


def as_of_date(series_map: dict) -> str | None:
    """Last date present for the majority of series (later date wins ties)."""
    last_dates = Counter(s["dates"][-1] for s in series_map.values() if s.get("dates"))
    if not last_dates:
        return None
    return max(last_dates.items(), key=lambda item: (item[1], item[0]))[0]


# ========================================================== trading calendar
#
# No extra dependency: US (NYSE) and EU (Xetra) holidays are hardcoded for
# 2026 and 2027, DST switch-overs are computed from the standard rules, and
# everything else is weekend logic. Outside CALENDAR_YEARS the helper falls
# back to weekends only and says so in meta.json `notes`.

CALENDAR_YEARS = (2026, 2027)

# NYSE full closures. Order per year: New Year, MLK, Washington's Birthday,
# Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
# Thanksgiving, Christmas — "observed" dates already applied where the real
# date falls on a weekend.
US_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})

# NYSE 13:00 ET early closes (day after Thanksgiving, Christmas Eve when it
# is a trading day). Used only to relax the partial-bar guard; a missing
# entry keeps the guard conservative — it would drop a complete bar, never
# ship an incomplete one.
US_EARLY_CLOSES = frozenset({"2026-11-27", "2026-12-24", "2027-11-26"})

# Xetra / Frankfurt full closures. Order per year: New Year, Good Friday,
# Easter Monday, Labour Day (2027: a Saturday, so absent), Whit Monday,
# Christmas Eve, Christmas Day (2027: a Saturday, so absent), New Year's Eve.
EU_HOLIDAYS = frozenset({
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-05-25",
    "2026-12-24", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-17", "2027-12-24",
    "2027-12-31",
})

MARKETS = ("US", "EU")


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """nth (1-based) `weekday` (Mon=0) of month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last `weekday` (Mon=0) of month."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def us_dst_active(day: date) -> bool:
    """US DST: 2nd Sunday in March .. 1st Sunday in November."""
    start = _nth_weekday(day.year, 3, 6, 2)
    end = _nth_weekday(day.year, 11, 6, 1)
    return start <= day < end


def eu_dst_active(day: date) -> bool:
    """EU DST: last Sunday in March .. last Sunday in October."""
    start = _last_weekday(day.year, 3, 6)
    end = _last_weekday(day.year, 10, 6)
    return start <= day < end


def calendar_covers(day: date) -> bool:
    return day.year in CALENDAR_YEARS


def is_trading_day(market: str, day: date) -> bool:
    """True when `market` holds a regular session on `day`."""
    if day.weekday() >= 5:
        return False
    key = day.isoformat()
    holidays = US_HOLIDAYS if market == "US" else EU_HOLIDAYS
    return key not in holidays


def market_close_utc(market: str, day: date) -> datetime:
    """UTC timestamp of `day`'s regular close for `market`."""
    if market == "US":
        if day.isoformat() in US_EARLY_CLOSES:      # 13:00 ET
            hour, minute = (17, 0) if us_dst_active(day) else (18, 0)
        else:                                        # 16:00 ET
            hour, minute = (20, 0) if us_dst_active(day) else (21, 0)
    else:                                            # Xetra 17:30 local
        hour, minute = (15, 30) if eu_dst_active(day) else (16, 30)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def market_has_closed(market: str, day: date, now: datetime) -> bool:
    return now >= market_close_utc(market, day)


def expected_last_trading_day(market: str, now: datetime) -> str | None:
    """Most recent session of `market` whose close has already passed."""
    day = now.date()
    for _ in range(30):
        if is_trading_day(market, day) and market_has_closed(market, day, now):
            return day.isoformat()
        day -= timedelta(days=1)
    return None


def sessions_between(market: str, start_iso: str | None, end_iso: str | None) -> int | None:
    """Number of `market` sessions strictly after start, up to and incl. end."""
    if not start_iso or not end_iso:
        return None
    try:
        start = date.fromisoformat(str(start_iso)[:10])
        end = date.fromisoformat(str(end_iso)[:10])
    except ValueError:
        return None
    if end <= start:
        return 0
    count, day, guard = 0, start + timedelta(days=1), 0
    while day <= end and guard < 800:
        if is_trading_day(market, day):
            count += 1
        day += timedelta(days=1)
        guard += 1
    return count


def market_of(ticker: str, eu_extra: frozenset[str]) -> str:
    """'EU' for European-listed tickers/indices, 'US' for everything else."""
    upper = str(ticker).upper()
    if upper in eu_extra:
        return "EU"
    return "EU" if upper.endswith(EU_SUFFIXES) else "US"


def drop_partial_last_bar(last_day: date, market: str, now: datetime) -> bool:
    """True when the last bar belongs to a session that has not closed yet."""
    if last_day != now.date():
        return False
    if not is_trading_day(market, last_day):
        return False
    return not market_has_closed(market, last_day, now)


# ================================================================== universe

def load_universe() -> dict:
    universe = yaml.safe_load(UNIVERSE_FILE.read_text(encoding="utf-8"))
    if not isinstance(universe, dict):
        raise ValueError("universe.yaml did not parse to a mapping")
    for key in ("etfs_us", "etfs_eu", "indices_eu", "fx", "vol",
                "stocks_eu", "stocks_us", "fred_series"):
        if key not in universe:
            raise ValueError(f"universe.yaml is missing the '{key}' section")
    return universe


def parse_constituents(text: str) -> list[str]:
    """Extract sorted, de-duplicated symbols from the constituents CSV."""
    table = pd.read_csv(io.StringIO(text))
    symbol_col = next((c for c in table.columns
                       if str(c).strip().lower() == "symbol"), table.columns[0])
    symbols = [str(s).strip() for s in table[symbol_col]]
    return sorted({s for s in symbols if s and s.lower() != "nan"})


def resolve_sp500(notes: list[str]) -> list[str]:
    """Return current S&P 500 tickers in Yahoo notation, [] if unavailable.

    Tries the live constituents CSV first and refreshes the cached copy in
    data/ on success; falls back to the cached copy otherwise.
    """
    symbols = None
    try:
        text = http_get_text(SP500_URL)
        symbols = parse_constituents(text)
        if len(symbols) < SP500_MIN_ROWS:
            raise ValueError(f"only {len(symbols)} symbols parsed, expected ~503")
        SP500_CACHE.write_text(text, encoding="utf-8")
        log.info("S&P 500 constituents downloaded: %d symbols", len(symbols))
    except Exception as exc:
        log.warning("S&P 500 constituents download failed: %s", exc)
        symbols = None
        if SP500_CACHE.exists():
            try:
                symbols = parse_constituents(SP500_CACHE.read_text(encoding="utf-8"))
                notes.append("stocks_us: live constituents fetch failed; "
                             "used cached data/sp500_constituents.csv")
                log.info("S&P 500 constituents from cache: %d symbols", len(symbols))
            except Exception as cache_exc:
                log.warning("cached constituents unreadable: %s", cache_exc)
    if not symbols:
        notes.append("stocks_us: constituents unavailable (download failed, "
                     "no usable cache); US stocks skipped this run")
        return []
    # Yahoo writes share classes with '-' where the CSV uses '.' (BRK.B -> BRK-B).
    return sorted({s.replace(".", "-") for s in symbols})


# ===================================================================== yahoo

def _download_batch(batch: list[str], interval: str, period: str):
    """One yf.download call with a single retry. Returns a DataFrame or None."""
    kwargs = {
        "interval": interval,
        "period": period,
        "auto_adjust": True,
        "actions": False,
        "group_by": "ticker",
        "threads": True,
        "progress": False,
    }
    for attempt in (1, 2):
        try:
            return yf.download(batch, **kwargs)
        except Exception as exc:
            log.warning("yf.download failed (attempt %d/2, %d tickers, %s %s): %s",
                        attempt, len(batch), interval, period, exc)
            if attempt == 1:
                time.sleep(RETRY_PAUSE)
    return None


def _split_frame(frame, batch: list[str]) -> dict:
    """Split one yf.download result into per-ticker DataFrames.

    With group_by='ticker' the columns are a MultiIndex of (ticker, field);
    a single ticker may come back with flat columns. Both shapes handled.
    """
    result = {}
    if frame is None or len(frame) == 0:
        return result
    if isinstance(frame.columns, pd.MultiIndex):
        present = set(frame.columns.get_level_values(0))
        for ticker in batch:
            if ticker in present:
                result[ticker] = frame[ticker]
    elif len(batch) == 1:
        result[batch[0]] = frame
    else:
        log.warning("unexpected flat columns for a %d-ticker batch; batch skipped",
                    len(batch))
    return result


def clean_frame(frame, market: str, now: datetime) -> tuple[pd.DataFrame | None, bool]:
    """Normalise one ticker's raw frame and apply the partial-bar guard.

    NaN-close rows are dropped (never forward-filled), duplicate dates keep
    the last row, the index is naive UTC dates, and a still-running session's
    bar is removed. Returns (frame_or_None, partial_bar_dropped).
    """
    if frame is None or len(frame) == 0 or "Close" not in frame.columns:
        return None, False
    out = frame.copy()
    index = pd.to_datetime(out.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    out.index = index.normalize()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in out.columns:
            values = pd.to_numeric(out[column], errors="coerce").astype("float64")
            # +/-inf is as unusable as NaN and would break strict JSON output
            out[column] = values.where(np.isfinite(values))
    out = out[out["Close"].notna()]
    if out.empty:
        return None, False
    out = out[~out.index.duplicated(keep="last")].sort_index()
    dropped = drop_partial_last_bar(out.index[-1].date(), market, now)
    if dropped:
        out = out.iloc[:-1]
    if out.empty:
        return None, dropped
    return out, dropped


def frame_to_series(df: pd.DataFrame, *, include_volume: bool = False,
                    include_hl: bool = False, last_n: int | None = None) -> dict:
    """Turn a cleaned frame into {dates, close[, volume][, high, low]} lists."""
    view = df.tail(last_n) if last_n is not None else df
    series = {
        "dates": [d.strftime("%Y-%m-%d") for d in view.index],
        "close": [round(float(v), 4) for v in view["Close"]],
    }
    if include_hl:
        for name, column in (("high", "High"), ("low", "Low")):
            if column in view.columns:
                values = view[column].astype("float64")
                fallback = view["Close"].astype("float64")
                values = values.where(np.isfinite(values), fallback)
                series[name] = [round(float(v), 4) for v in values]
            else:
                series[name] = [round(float(v), 4) for v in view["Close"]]
    if include_volume:
        if "Volume" in view.columns:
            volume = pd.to_numeric(view["Volume"], errors="coerce").fillna(0)
            series["volume"] = [int(v) for v in volume]
        else:
            series["volume"] = [0] * len(series["dates"])
    return series


def download_frames(tickers: list[str], interval: str, period: str,
                    market_lookup, now: datetime,
                    label: str = "") -> tuple[dict, list[str], list[str]]:
    """Download all tickers in batches.

    Returns (frames_by_ticker, failed, partial_bar_dropped).
    """
    frames: dict[str, pd.DataFrame] = {}
    guarded: list[str] = []
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for number, batch in enumerate(batches, start=1):
        log.info("%s: batch %d/%d (%d tickers)", label, number, len(batches), len(batch))
        raw = _download_batch(batch, interval, period)
        for ticker, sub in _split_frame(raw, batch).items():
            try:
                cleaned, dropped = clean_frame(sub, market_lookup(ticker), now)
            except Exception as exc:
                log.warning("%s: could not clean %s: %s", label, ticker, exc)
                cleaned, dropped = None, False
            if dropped:
                guarded.append(ticker)
            if cleaned is not None:
                frames[ticker] = cleaned
        if number < len(batches):
            time.sleep(BATCH_PAUSE)
    failed = sorted(set(tickers) - set(frames))
    if failed:
        shown = ", ".join(failed[:20]) + (" ..." if len(failed) > 20 else "")
        log.warning("%s: no data for %d/%d tickers: %s",
                    label, len(failed), len(tickers), shown)
    if guarded:
        log.info("%s: partial-bar guard dropped the last bar of %d tickers",
                 label, len(guarded))
    return frames, failed, sorted(guarded)


def resample_weekly(df: pd.DataFrame) -> dict:
    """Weekly (W-FRI) last close resampled from the daily frame.

    Labels are the Friday that ends each week — including the current, still
    running week, whose label can therefore be a future date. The caller
    ships the last daily date alongside so that is never ambiguous.
    """
    weekly = df["Close"].astype("float64").resample("W-FRI").last().dropna()
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in weekly.index],
        "close": [round(float(v), 4) for v in weekly],
    }


# ========================================================== derived metrics

DERIVED_FIELDS = (
    "last_close", "last_date",
    "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_12m", "mom_12_1",
    "sma50", "sma200", "pct_vs_sma50", "pct_vs_sma200", "sma200_slope_20d",
    "high_52w", "low_52w", "pct_from_52w_high", "pct_above_52w_low",
    "adv20", "rvol", "vol_20d_ann", "vol_60d_ann",
    "atr14_pct", "drawdown_from_252d_high",
    "above_sma200", "above_sma50", "new_52w_high_20d",
)

RETURN_LOOKBACKS = {"ret_1w": 5, "ret_1m": 21, "ret_3m": 63,
                    "ret_6m": 126, "ret_12m": 252}
WINDOW_52W = 252
TRADING_DAYS_PER_YEAR = 252


def _tail_finite(series: pd.Series | None, count: int) -> bool:
    """True when the last `count` values of `series` are all finite."""
    if series is None or len(series) < count:
        return False
    return bool(np.isfinite(series.iloc[-count:].to_numpy()).all())


def compute_derived(df: pd.DataFrame) -> dict:
    """Per-ticker momentum / trend / volatility metrics from a daily frame.

    Every field is present; a field is null whenever the history is too short
    for its definition. Nothing is extrapolated or back-filled.
    """
    metrics: dict[str, object] = {field: None for field in DERIVED_FIELDS}
    if df is None or df.empty or "Close" not in df.columns:
        return metrics

    close = df["Close"].astype("float64")
    n = len(close)
    last_close = float(close.iloc[-1])
    metrics["last_close"] = r4(last_close)
    metrics["last_date"] = df.index[-1].strftime("%Y-%m-%d")

    high = df["High"].astype("float64") if "High" in df.columns else None
    low = df["Low"].astype("float64") if "Low" in df.columns else None
    volume = df["Volume"].astype("float64") if "Volume" in df.columns else None

    # --- simple total returns over trading-day lookbacks -----------------
    for field, lookback in RETURN_LOOKBACKS.items():
        if n >= lookback + 1:
            metrics[field] = pct_of(last_close, close.iloc[-1 - lookback])

    # --- 12-1 momentum: t-252 -> t-21 ------------------------------------
    if n >= 253:
        metrics["mom_12_1"] = pct_of(close.iloc[-22], close.iloc[-253])

    # --- moving averages --------------------------------------------------
    if n >= 50:
        sma50 = float(close.iloc[-50:].mean())
        metrics["sma50"] = r4(sma50)
        metrics["pct_vs_sma50"] = pct_of(last_close, sma50)
        metrics["above_sma50"] = bool(last_close > sma50)
    if n >= 200:
        sma200_series = close.rolling(200, min_periods=200).mean()
        sma200 = float(sma200_series.iloc[-1])
        metrics["sma200"] = r4(sma200)
        metrics["pct_vs_sma200"] = pct_of(last_close, sma200)
        metrics["above_sma200"] = bool(last_close > sma200)
        if n >= 220:
            metrics["sma200_slope_20d"] = pct_of(sma200_series.iloc[-1],
                                                 sma200_series.iloc[-21])

    # --- 52-week extremes (true intraday highs/lows when available) -------
    # A NaN anywhere inside the window disqualifies High/Low and the metric
    # falls back to closes rather than silently using a shorter window.
    if n >= WINDOW_52W:
        high_source = high if _tail_finite(high, WINDOW_52W) else close
        low_source = low if _tail_finite(low, WINDOW_52W) else close
        high_52w = float(high_source.iloc[-WINDOW_52W:].max())
        low_52w = float(low_source.iloc[-WINDOW_52W:].min())
        metrics["high_52w"] = r4(high_52w)
        metrics["low_52w"] = r4(low_52w)
        metrics["pct_from_52w_high"] = pct_of(last_close, high_52w)
        metrics["pct_above_52w_low"] = pct_of(last_close, low_52w)
        metrics["drawdown_from_252d_high"] = pct_of(
            last_close, float(close.iloc[-WINDOW_52W:].max()))

    # --- new 252-day high inside the last 20 sessions ---------------------
    if n >= WINDOW_52W + 19:
        span = WINDOW_52W + 19
        high_source = high if _tail_finite(high, span) else close
        rolling_high = high_source.rolling(WINDOW_52W, min_periods=WINDOW_52W).max()
        recent = high_source.iloc[-20:].to_numpy()
        window = rolling_high.iloc[-20:].to_numpy()
        if np.isfinite(window).all():
            metrics["new_52w_high_20d"] = bool(np.any(recent >= window - 1e-9))

    # --- liquidity --------------------------------------------------------
    if volume is not None and n >= 20:
        dollar = (close * volume).iloc[-20:]
        if np.isfinite(dollar.to_numpy()).all():
            metrics["adv20"] = r4(float(dollar.mean()))
    if volume is not None and n >= 21:
        baseline = float(volume.iloc[-21:-1].mean())
        last_volume = float(volume.iloc[-1])
        if np.isfinite(baseline) and baseline > 0 and np.isfinite(last_volume):
            metrics["rvol"] = round(last_volume / baseline, 4)

    # --- realised volatility ---------------------------------------------
    if n >= 21 and float(close.min()) > 0:
        log_returns = np.log(close / close.shift(1)).dropna()
        for field, window in (("vol_20d_ann", 20), ("vol_60d_ann", 60)):
            if len(log_returns) >= window:
                sample = log_returns.iloc[-window:]
                stdev = float(sample.std(ddof=1))
                if np.isfinite(stdev):
                    metrics[field] = round(
                        stdev * np.sqrt(TRADING_DAYS_PER_YEAR) * 100.0, 4)

    # --- ATR(14) as a percentage of the last close ------------------------
    if _tail_finite(high, 15) and _tail_finite(low, 15) and last_close > 0:
        previous_close = close.shift(1)
        true_range = pd.concat([
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(true_range.iloc[-14:].mean())
        if np.isfinite(atr14):
            metrics["atr14_pct"] = round(atr14 / last_close * 100.0, 4)

    return metrics


def build_derived(frames: dict) -> dict:
    """derived.json payload for every ticker with usable daily history."""
    metrics = {}
    for ticker in sorted(frames):
        try:
            metrics[ticker] = compute_derived(frames[ticker])
        except Exception as exc:
            log.warning("derived metrics failed for %s: %s", ticker, exc)
    last_dates = Counter(m["last_date"] for m in metrics.values() if m.get("last_date"))
    as_of = (max(last_dates.items(), key=lambda i: (i[1], i[0]))[0]
             if last_dates else None)
    return {"as_of": as_of, "metrics": metrics}


# ============================================================ breadth history

def _region_flags(close: pd.Series) -> dict[str, pd.Series]:
    """Per-ticker boolean inputs for the breadth aggregation."""
    n = len(close)
    eligible = pd.Series(np.arange(n) >= 199, index=close.index)
    sma200 = close.rolling(200, min_periods=200).mean()
    sma50 = close.rolling(50, min_periods=50).mean()
    high20 = close.rolling(20, min_periods=20).max()
    low20 = close.rolling(20, min_periods=20).min()
    return {
        "eligible": eligible,
        "above200": eligible & (close > sma200),
        "above50": eligible & (close > sma50),
        "new_high": eligible & (close >= high20 - 1e-9),
        "new_low": eligible & (close <= low20 + 1e-9),
    }


def _aligned(flag_map: dict, key: str, index) -> pd.DataFrame:
    """Wide float frame of one flag, aligned on `index` (absent -> 0)."""
    columns = {ticker: flags[key].astype("float64")
               for ticker, flags in flag_map.items()}
    if not columns:
        return pd.DataFrame(index=index)
    return pd.DataFrame(columns).reindex(index).fillna(0.0)


def compute_breadth_region(frames: dict, tickers: list[str],
                           sessions: int = BREADTH_SESSIONS) -> dict:
    """Participation history for one region, vectorised over a wide panel."""
    empty = {"dates": [], "pct_above_sma200": [], "pct_above_sma50": [],
             "net_new_highs_20d": [], "n_eligible": []}
    usable = [t for t in tickers if t in frames and len(frames[t]) >= 20]
    if not usable:
        return empty

    closes = {t: frames[t]["Close"].astype("float64") for t in usable}
    panel = pd.DataFrame(closes)
    coverage = panel.notna().sum(axis=1)
    if coverage.empty or int(coverage.max()) == 0:
        return empty
    # Keep only dates where at least half the region actually traded, so a
    # single exchange's odd session does not create a phantom breadth point.
    threshold = max(1, int(0.5 * int(coverage.max())))
    index = coverage.index[coverage >= threshold].sort_values()[-sessions:]
    if len(index) == 0:
        return empty

    flag_map = {t: _region_flags(closes[t]) for t in usable}
    eligible = _aligned(flag_map, "eligible", index).sum(axis=1)
    above200 = _aligned(flag_map, "above200", index).sum(axis=1)
    above50 = _aligned(flag_map, "above50", index).sum(axis=1)
    new_high = _aligned(flag_map, "new_high", index).sum(axis=1)
    new_low = _aligned(flag_map, "new_low", index).sum(axis=1)

    # Drop the leading run of sessions where nothing was eligible yet (only
    # happens when the panel is barely longer than the 200-day requirement);
    # a date is kept as soon as at least one ticker qualifies.
    counts = eligible.to_numpy()
    first = int(np.argmax(counts > 0)) if bool((counts > 0).any()) else len(counts)
    if first >= len(counts):
        return empty

    def as_pct(numerator: pd.Series) -> list:
        values = []
        for count, base in zip(numerator.to_numpy()[first:], counts[first:]):
            values.append(round(float(count) / float(base) * 100.0, 4)
                          if base > 0 else None)
        return values

    return {
        "dates": [d.strftime("%Y-%m-%d") for d in index[first:]],
        "pct_above_sma200": as_pct(above200),
        "pct_above_sma50": as_pct(above50),
        "net_new_highs_20d": as_pct(new_high - new_low),
        "n_eligible": [int(v) for v in counts[first:]],
    }


def build_breadth(frames: dict, region: dict) -> dict:
    payload: dict[str, object] = {}
    for name in ("US", "EU"):
        tickers = sorted(t for t, reg in region.items() if reg == name)
        payload[name] = compute_breadth_region(frames, tickers)
        log.info("breadth %s: %d sessions, %d tickers considered",
                 name, len(payload[name]["dates"]), len(tickers))
    dates = [payload[name]["dates"][-1] for name in ("US", "EU")
             if payload[name]["dates"]]
    payload["as_of"] = max(dates) if dates else None
    return payload


# ====================================================== extra market sources

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return r4(value)
    match = _NUMBER_RE.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        return r4(float(match.group(0)))
    except ValueError:
        return None


def _to_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _epoch_to_date(value) -> str | None:
    """CNN uses epoch milliseconds; accept seconds and ISO strings too."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            return pd.to_datetime(str(value)).strftime("%Y-%m-%d")
        except Exception:
            return None
    if not np.isfinite(number):
        return None
    seconds = number / 1000.0 if abs(number) > 1e11 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def parse_cnn_fear_greed(payload) -> dict:
    """CNN Fear & Greed composite, components and historical series.

    Component keys are read off the payload rather than assumed, so a CNN
    rename shows up as a changed `component_keys` list instead of silently
    missing data.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected payload type {type(payload).__name__}")

    composite = payload.get("fear_and_greed")
    composite = composite if isinstance(composite, dict) else {}
    result: dict[str, object] = {
        "score": _to_float(composite.get("score")),
        "rating": _to_text(composite.get("rating")),
        "timestamp": _to_text(composite.get("timestamp")),
    }

    components = {}
    for key, value in payload.items():
        if key in ("fear_and_greed", "fear_and_greed_historical"):
            continue
        if isinstance(value, dict) and ("score" in value or "rating" in value):
            components[key] = {
                "score": _to_float(value.get("score")),
                "rating": _to_text(value.get("rating")),
                "timestamp": _to_text(value.get("timestamp")),
            }
    result["components"] = components
    result["component_keys"] = sorted(components)

    historical = payload.get("fear_and_greed_historical")
    points = historical.get("data") if isinstance(historical, dict) else historical
    dates, values = [], []
    if isinstance(points, list):
        for point in points:
            if not isinstance(point, dict):
                continue
            stamp = point.get("x", point.get("timestamp"))
            value = point.get("y", point.get("score"))
            day, number = _epoch_to_date(stamp), _to_float(value)
            if day is None or number is None:
                continue
            dates.append(day)
            values.append(number)
    result["historical"] = {"dates": dates[-CNN_HISTORY_POINTS:],
                            "values": values[-CNN_HISTORY_POINTS:]}
    if result["score"] is None and not components:
        raise ValueError("no composite score and no components recognised")
    return result


def fetch_cnn_fear_greed() -> dict:
    return parse_cnn_fear_greed(http_get(CNN_FEAR_GREED_URL, browser=True).json())


_PUTCALL_LABEL_RE = re.compile(r"put\s*/?\s*call\s+ratio", re.IGNORECASE)
_PUTCALL_TEXT_RE = re.compile(
    r"([A-Za-z0-9()&/.,'\- ]{2,60}?put\s*/?\s*call\s+ratio)"
    r"[^0-9\-]{0,40}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:January|February|March|April|May|June|July|August|"
               r"September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
               re.IGNORECASE),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
)


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"<[^>]+>", "\n", text)
    return html_module.unescape(text)


def _normalise_label(label: str) -> str:
    return re.sub(r"\s+", " ", str(label)).strip().upper()


_DATE_CONTEXT_RE = re.compile(r"(?:statistics for|report date|as of|date)\b",
                              re.IGNORECASE)


def _find_report_date(text: str) -> str | None:
    """First date string near a date-ish keyword, else the first one at all.

    Returned verbatim and unparsed: it is a label for the reader, never used
    for any calculation.
    """
    windows = [text[m.end():m.end() + 120] for m in _DATE_CONTEXT_RE.finditer(text)]
    for chunk in windows + [text]:
        for pattern in _DATE_PATTERNS:
            match = pattern.search(chunk)
            if match:
                return match.group(0).strip()
    return None


def parse_cboe_putcall(markup: str) -> dict:
    """Parse the CBOE daily statistics page for whatever ratios it lists.

    No expected set is hardcoded: any "<something> PUT/CALL RATIO" label with
    a numeric neighbour is kept. pandas.read_html first, regex over the
    stripped text as a fallback, and both results merged.
    """
    ratios: dict[str, float] = {}
    method = []

    try:
        tables = pd.read_html(io.StringIO(markup))
    except Exception as exc:                      # noqa: BLE001 - page shape varies
        tables = []
        log.info("cboe: read_html found no tables (%s)", exc)
    for table in tables:
        frame = table.astype("object")
        for row in frame.itertuples(index=False, name=None):
            cells = [c for c in row if c is not None and str(c).strip().lower() != "nan"]
            label = next((str(c) for c in cells if _PUTCALL_LABEL_RE.search(str(c))), None)
            if label is None:
                continue
            value = None
            for cell in cells:
                if str(cell) == label:
                    continue
                candidate = _to_float(cell)
                if candidate is not None:
                    value = candidate
                    break
            if value is not None:
                ratios.setdefault(_normalise_label(label), value)
    if ratios:
        method.append("pandas.read_html")

    text = _strip_html(markup)
    before = len(ratios)
    for label, value in _PUTCALL_TEXT_RE.findall(text):
        parsed = _to_float(value)
        if parsed is not None:
            ratios.setdefault(_normalise_label(label), parsed)
    if len(ratios) > before:
        method.append("regex")

    report_date = _find_report_date(text)

    if not ratios:
        raise ValueError("no put/call rows recognised; page shape may have changed")
    return {"ratios": ratios, "labels": sorted(ratios),
            "report_date_raw": report_date, "parsed_with": method}


def fetch_cboe_putcall() -> dict:
    return parse_cboe_putcall(http_get_text(CBOE_PUTCALL_URL, browser=True))


_MONEY_RE = re.compile(r"^\s*([+-])?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMB])?\s*$",
                       re.IGNORECASE)
_MONEY_SCALE = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_money(raw) -> int | None:
    """'+$1,473,069' -> 1473069. Handles K/M/B suffixes and negatives."""
    text = _to_text(raw)
    if text is None or text.lower() in ("nan", "none", "-", "n/a"):
        return None
    negative = text.strip().startswith("(") and text.strip().endswith(")")
    body = text.strip("()").strip()
    match = _MONEY_RE.match(body)
    if match:
        sign, digits, suffix = match.groups()
        value = float(digits.replace(",", ""))
        if suffix:
            value *= _MONEY_SCALE[suffix.upper()]
        if sign == "-" or negative:
            value = -value
        return int(round(value))
    digits = re.sub(r"[^0-9.]", "", body)
    if not digits or digits.count(".") > 1:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if body.lstrip().startswith("-") or negative:
        value = -value
    return int(round(value))


def _normalise_key(name) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _pick_column(columns, *candidates, exclude=()) -> object | None:
    """Exact normalised match first, then substring, over the header names."""
    lookup = {column: _normalise_key(column) for column in columns
              if column not in exclude}
    for candidate in candidates:
        target = _normalise_key(candidate)
        for column, key in lookup.items():
            if key == target:
                return column
    for candidate in candidates:
        target = _normalise_key(candidate)
        if not target:
            continue
        for column, key in lookup.items():
            if target in key:
                return column
    return None


def _flatten_columns(table: pd.DataFrame) -> pd.DataFrame:
    """read_html can hand back a MultiIndex header; join it into one label."""
    if isinstance(table.columns, pd.MultiIndex):
        table = table.copy()
        table.columns = [" ".join(str(p) for p in tup
                                  if str(p) and not str(p).startswith("Unnamed"))
                         for tup in table.columns]
    return table


def parse_openinsider(markup: str, max_rows: int = OPENINSIDER_MAX_ROWS) -> dict:
    """Parse openinsider's cluster-buys table into normalised rows."""
    tables = [_flatten_columns(t) for t in pd.read_html(io.StringIO(markup))]
    candidates = [t for t in tables
                  if _pick_column(t.columns, "Filing Date") is not None
                  and _pick_column(t.columns, "Ticker") is not None]
    if not candidates:
        raise ValueError("no table with Filing Date + Ticker columns found")
    table = max(candidates, key=len)

    mapping: dict[str, object] = {}
    wanted = (
        ("filing_date", ("Filing Date", "Filing")),
        ("trade_date", ("Trade Date",)),
        ("ticker", ("Ticker", "Symbol")),
        ("company", ("Company Name", "Company")),
        ("insiders_count", ("Insiders", "Insider Count", "# Insiders")),
        ("trade_type", ("Trade Type", "Type")),
        ("price", ("Price",)),
        ("qty", ("Qty", "Quantity")),
        ("owned", ("Owned",)),
        # openinsider labels this column "ΔOwn"; the delta character is
        # stripped by normalisation, so match the exact key "own" before the
        # substring pass can grab "Owned".
        ("delta_own", ("Own", "DeltaOwn", "Change in Ownership")),
        ("value_usd", ("Value",)),
    )
    for field, names in wanted:
        mapping[field] = _pick_column(table.columns, *names,
                                      exclude=set(mapping.values()))
    missing = sorted(k for k, v in mapping.items() if v is None)

    rows = []
    for record in table.head(max_rows * 3).to_dict("records"):
        def cell(field, _record=record):
            column = mapping.get(field)
            return _record.get(column) if column is not None else None

        ticker = _to_text(cell("ticker"))
        if not ticker or ticker.lower() == "nan" or len(ticker) > 12:
            continue
        value_raw = _to_text(cell("value_usd"))
        rows.append({
            "filing_date": _to_text(cell("filing_date")),
            "trade_date": _to_text(cell("trade_date")),
            "ticker": ticker.upper(),
            "company": _to_text(cell("company")),
            "insiders_count": parse_money(cell("insiders_count")),
            "trade_type": _to_text(cell("trade_type")),
            "price": _to_float(cell("price")),
            "qty": parse_money(cell("qty")),
            "owned": parse_money(cell("owned")),
            "delta_own": _to_text(cell("delta_own")),
            "value_usd": parse_money(value_raw),
            "value_raw": value_raw,
        })
        if len(rows) >= max_rows:
            break
    if not rows:
        raise ValueError("cluster-buys table parsed but contained no usable rows")
    return {"source": OPENINSIDER_SOURCE_TAG, "row_count": len(rows),
            "columns_missing": missing, "rows": rows}


def fetch_openinsider() -> dict:
    return parse_openinsider(http_get_text(OPENINSIDER_URL, browser=True))


def collect_market_sources() -> tuple[dict, dict]:
    """Run the three best-effort sources. Returns (sources, status)."""
    sources: dict[str, dict] = {}
    status: dict[str, str] = {}
    jobs = (("cnn_fear_greed", fetch_cnn_fear_greed),
            ("cboe_putcall", fetch_cboe_putcall),
            ("insider_cluster_buys", fetch_openinsider))
    for name, fetch in jobs:
        started = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            payload = fetch()
            payload.update({"ok": True, "fetched_utc": started})
            sources[name] = payload
            status[name] = "ok"
            log.info("market source %s: ok", name)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            sources[name] = {"ok": False, "fetched_utc": started, "error": detail}
            status[name] = f"failed: {detail}"
            log.warning("market source %s failed: %s", name, detail)
        time.sleep(1)
    return sources, status


# ======================================================================= fred

def fetch_fred(series_ids: list[str]) -> tuple[dict, list[str]]:
    """Fetch each FRED series as CSV. Returns (series_by_id, failed_ids)."""
    series, failed = {}, []
    start = (utc_now() - timedelta(days=366 * FRED_YEARS)).strftime("%Y-%m-%d")
    for sid in series_ids:
        try:
            text = http_get_text(FRED_URL, params={"id": sid, "cosd": start})
            table = pd.read_csv(io.StringIO(text))
            if table.shape[1] < 2:
                raise ValueError(f"unexpected CSV shape {table.shape}")
            date_col = table.columns[0]      # 'observation_date' (was 'DATE')
            value_col = next((c for c in table.columns[1:]
                              if str(c).strip().upper() == sid.upper()),
                             table.columns[1])
            values = pd.to_numeric(table[value_col], errors="coerce")  # '.' -> NaN
            keep = values.notna()
            dates = pd.to_datetime(table.loc[keep, date_col]).dt.strftime("%Y-%m-%d")
            series[sid] = {
                "dates": list(dates),
                "values": [round(float(v), 4) for v in values[keep]],
            }
            log.info("FRED %s: %d observations", sid, int(keep.sum()))
        except Exception as exc:
            log.warning("FRED %s failed: %s", sid, exc)
            failed.append(sid)
        time.sleep(1)
    return series, failed


# ================================================================== freshness

def modal_last_bar(last_bar_dates: dict, market_lookup, market: str) -> str | None:
    """Most common last-bar date among the tickers of one market."""
    counts = Counter(day for ticker, day in last_bar_dates.items()
                     if day and market_lookup(ticker) == market)
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def build_freshness(last_bar_dates: dict, market_lookup, expected: dict) -> dict:
    freshness = {}
    for market in MARKETS:
        actual = modal_last_bar(last_bar_dates, market_lookup, market)
        freshness[market] = {
            "expected": expected.get(market),
            "actual": actual,
            "lag_sessions": sessions_between(market, actual, expected.get(market)),
        }
    return freshness


def find_stale_tickers(last_bar_dates: dict, market_lookup, expected: dict) -> list[str]:
    stale = []
    for ticker, day in sorted(last_bar_dates.items()):
        market = market_lookup(ticker)
        lag = sessions_between(market, day, expected.get(market))
        if lag is not None and lag > STALE_LAG_SESSIONS:
            stale.append(ticker)
    return stale


# ======================================================================= main

def run(run_utc: str) -> tuple[int, dict]:
    """Do the whole collection. Returns (exit_code, meta_payload)."""
    now = utc_now()
    DATA_DIR.mkdir(exist_ok=True)

    universe = load_universe()
    notes: list[str] = []
    failed_tickers: dict[str, list[str]] = {}
    dropped_tickers: dict[str, list[str]] = {}

    def record_failed(bucket: str, tickers: list[str]) -> None:
        if tickers:
            failed_tickers[bucket] = sorted(
                set(failed_tickers.get(bucket, [])) | set(tickers))

    # --- assemble ticker lists and remember each ticker's bucket ---------
    bucket_of: dict[str, str] = {}
    etf_tickers: list[str] = []
    for bucket in ("etfs_us", "etfs_eu"):
        for entry in universe[bucket]:
            ticker = str(entry["ticker"])
            etf_tickers.append(ticker)
            bucket_of[ticker] = bucket
    eu_index_tickers = {str(t).upper() for t in universe["indices_eu"]}
    for bucket in ("indices_eu", "fx", "vol"):
        for ticker in universe[bucket]:
            ticker = str(ticker)
            etf_tickers.append(ticker)
            bucket_of[ticker] = bucket

    eu_extra = frozenset(eu_index_tickers)

    def market_lookup(ticker: str) -> str:
        return market_of(ticker, eu_extra)

    stocks_eu = [str(entry["ticker"]) for entry in universe["stocks_eu"]]
    if universe["stocks_us"] == "auto:sp500":
        stocks_us = resolve_sp500(notes)
    elif isinstance(universe["stocks_us"], list):
        stocks_us = [str(t) for t in universe["stocks_us"]]
    else:
        notes.append(f"stocks_us: unrecognised spec {universe['stocks_us']!r}; skipped")
        stocks_us = []

    region: dict[str, str] = {}
    all_stocks: list[str] = []
    for ticker, bucket, reg in (
            [(t, "stocks_us", "US") for t in stocks_us]
            + [(t, "stocks_eu", "EU") for t in stocks_eu]):
        if ticker not in region:
            all_stocks.append(ticker)
            region[ticker] = reg
            bucket_of[ticker] = bucket

    log.info("universe: %d ETF/index/vol/FX tickers, %d stocks (%d US, %d EU), "
             "%d FRED series", len(etf_tickers), len(all_stocks),
             len(stocks_us), len(stocks_eu), len(universe["fred_series"]))

    if not calendar_covers(now.date()):
        notes.append(f"trading calendar: hardcoded holidays cover "
                     f"{CALENDAR_YEARS[0]}-{CALENDAR_YEARS[-1]} only; "
                     f"{now.year} falls back to weekend logic — refresh the lists")

    # --- downloads (two passes, both daily) ------------------------------
    etf_frames, etf_failed, etf_guarded = download_frames(
        etf_tickers, "1d", ETF_PERIOD, market_lookup, now,
        label=f"etf daily {ETF_PERIOD}")
    stock_frames, stock_failed, stock_guarded = download_frames(
        all_stocks, "1d", STOCK_PERIOD, market_lookup, now,
        label=f"stocks daily {STOCK_PERIOD}")

    for ticker in etf_failed + stock_failed:
        record_failed(bucket_of[ticker], [ticker])
    guarded = etf_guarded + stock_guarded
    if guarded:
        notes.append(f"partial-bar guard: dropped the in-progress session bar for "
                     f"{len(guarded)} tickers")

    fred_series, fred_failed = fetch_fred([str(s) for s in universe["fred_series"]])
    record_failed("fred", fred_failed)

    market_sources, market_status = collect_market_sources()
    for name, state in market_status.items():
        if state != "ok":
            notes.append(f"market.json source {name} {state}")

    # --- hygiene: drop stocks with too little daily history ---------------
    for ticker in sorted(stock_frames):
        if len(stock_frames[ticker]) < MIN_DAILY_POINTS:
            dropped_tickers.setdefault(bucket_of[ticker], []).append(ticker)
            del stock_frames[ticker]
    for bucket, tickers in dropped_tickers.items():
        notes.append(f"{bucket}: dropped {len(tickers)} tickers with fewer than "
                     f"{MIN_DAILY_POINTS} daily closes")

    # --- shipped price series ---------------------------------------------
    etf_daily = {t: frame_to_series(f, include_volume=True, include_hl=True)
                 for t, f in sorted(etf_frames.items())}
    etf_weekly = {t: resample_weekly(f) for t, f in sorted(etf_frames.items())}
    stocks_daily = {t: frame_to_series(f, include_volume=True,
                                       last_n=STOCKS_KEEP_POINTS)
                    for t, f in sorted(stock_frames.items())}

    all_frames = dict(etf_frames)
    all_frames.update(stock_frames)

    # --- derived metrics and breadth --------------------------------------
    derived = build_derived(all_frames)
    log.info("derived: %d tickers", len(derived["metrics"]))
    stock_region = {t: region[t] for t in stock_frames}
    breadth = build_breadth(stock_frames, stock_region)

    # --- freshness --------------------------------------------------------
    last_bar_dates = {t: f.index[-1].strftime("%Y-%m-%d")
                      for t, f in sorted(all_frames.items())}
    expected = {m: expected_last_trading_day(m, now) for m in MARKETS}
    freshness = build_freshness(last_bar_dates, market_lookup, expected)
    stale = find_stale_tickers(last_bar_dates, market_lookup, expected)
    if stale:
        notes.append(f"{len(stale)} tickers are more than {STALE_LAG_SESSIONS} "
                     f"sessions behind their market's expected last trading day")
    for market in MARKETS:
        log.info("freshness %s: expected %s, actual %s, lag %s sessions", market,
                 freshness[market]["expected"], freshness[market]["actual"],
                 freshness[market]["lag_sessions"])

    # --- per-bucket counts (measured on the bucket's primary file) --------
    ok_in: Counter = Counter()
    for ticker in etf_daily:
        ok_in[bucket_of[ticker]] += 1
    for ticker in stocks_daily:
        ok_in[bucket_of[ticker]] += 1
    ok_in["fred"] = len(fred_series)
    requested = {bucket: len(universe[bucket]) for bucket in
                 ("etfs_us", "etfs_eu", "indices_eu", "fx", "vol")}
    requested.update({"stocks_us": len(stocks_us), "stocks_eu": len(stocks_eu),
                      "fred": len(universe["fred_series"])})
    counts = {}
    for bucket, want in requested.items():
        ok = ok_in.get(bucket, 0)
        dropped = len(dropped_tickers.get(bucket, []))
        counts[bucket] = {"requested": want, "ok": ok, "dropped": dropped,
                          "failed": max(want - ok - dropped, 0)}

    # --- write outputs ----------------------------------------------------
    etf_as_of = as_of_date(etf_daily)
    write_json(DATA_DIR / "etf_daily.json",
               {"as_of": etf_as_of, "series": etf_daily})
    write_json(DATA_DIR / "etf_weekly.json",
               {"as_of": etf_as_of, "last_daily_date": etf_as_of,
                "series": etf_weekly})
    write_json(DATA_DIR / "stocks_daily.json",
               {"as_of": as_of_date(stocks_daily),
                "region": {t: region[t] for t in stocks_daily},
                "series": stocks_daily})
    write_json(DATA_DIR / "derived.json", derived)
    write_json(DATA_DIR / "breadth.json", breadth)
    write_json(DATA_DIR / "market.json",
               {"as_of": now.strftime("%Y-%m-%d"), "sources": market_sources})
    write_json(DATA_DIR / "fred.json",
               {"as_of": as_of_date(fred_series), "series": fred_series})

    # stocks_weekly.json was removed in schema 2 (weekly is resampled from
    # daily now); delete any leftover so nobody reads a frozen file.
    legacy = DATA_DIR / "stocks_weekly.json"
    if legacy.exists():
        legacy.unlink()
        notes.append("removed legacy data/stocks_weekly.json (schema 2 ships "
                     "etf_weekly.json; stock weekly series are gone)")

    total_ok = len(etf_daily) + len(stocks_daily)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "run_utc": run_utc,
        "run_ok": total_ok > 0,
        "as_of": etf_as_of,
        "adjustment": ADJUSTMENT_NOTE,
        "yfinance_version": yf.__version__,
        "pandas_version": pd.__version__,
        "numpy_version": np.__version__,
        "expected_last_trading_day": expected,
        "freshness": freshness,
        "last_bar_dates": last_bar_dates,
        "stale_tickers": stale,
        "partial_bar_guard_dropped": sorted(guarded),
        "counts": counts,
        "failed_tickers": failed_tickers,
        "dropped_tickers": dropped_tickers,
        "market_sources": market_status,
        "files": ["meta.json", "etf_daily.json", "etf_weekly.json",
                  "stocks_daily.json", "derived.json", "breadth.json",
                  "market.json", "fred.json"],
        "notes": notes,
    }
    log.info("done: %d ETF/index series, %d stock series, %d FRED series, "
             "%d derived tickers", len(etf_daily), len(stocks_daily),
             len(fred_series), len(derived["metrics"]))
    if total_ok == 0:
        log.error("no price data was downloaded at all; failing the run")
        return 1, meta
    return 0, meta


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    run_utc = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    DATA_DIR.mkdir(exist_ok=True)
    try:
        code, meta = run(run_utc)
    except Exception as exc:                       # noqa: BLE001 - heartbeat
        log.error("collector crashed: %s", exc)
        log.error("%s", traceback.format_exc())
        meta = {
            "schema_version": SCHEMA_VERSION,
            "run_utc": run_utc,
            "run_ok": False,
            "as_of": None,
            "adjustment": ADJUSTMENT_NOTE,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "notes": ["collector crashed before writing the data files; the "
                      "other files in data/ are from the previous successful run"],
        }
        code = 1
    try:
        write_json(DATA_DIR / "meta.json", meta)
    except Exception as exc:                       # noqa: BLE001 - last resort
        log.error("meta.json could not be serialised (%s); writing a stub", exc)
        write_json(DATA_DIR / "meta.json", {
            "schema_version": SCHEMA_VERSION, "run_utc": run_utc,
            "run_ok": False, "as_of": None,
            "error": f"meta serialisation failed: {type(exc).__name__}: {exc}"[:500],
            "notes": ["meta.json could not be serialised; see the Actions log"],
        })
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
