"""Live Market Regime Classifier -- EXACT port of Regime_Classifier_Design_v1.md
(the same classifier validated in Research_Protocol_v3_regime.md). Computes
trend_regime ("Trend"/"Range") from live Binance BTCUSDT H1 data, no MT5 --
faithful to the classifier as tested, not a re-derivation on a different feed.

Cold-start note: replays the hysteresis state machine over the last ~3000
H1 bars (~125 days, comfortably more than the 2160-bar vol baseline + ADX
warmup) rather than the full multi-year history used to build/test the
classifier. The hysteresis state is a bounded-memory state machine (next
state depends only on the previous state + current value), so after the
first classifiable bar of any sufficiently long window it converges to the
same trajectory a full-history replay would produce -- this is a
performance/practicality choice, not a logic change.
"""
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import numpy as np

BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"

ADX_LEN = 14
ADX_ENTER_TREND = 25
ADX_EXIT_TREND = 20
ATR_LEN = 14
VOL_WIN = 2160
VOL_ENTER_HIGH = 60
VOL_EXIT_HIGH = 40
FETCH_BARS = 3000  # >> VOL_WIN + warmup, cold-start replay window

_cache = {"h1_bar_time": None, "regime": None, "adx": None, "fetched_at": None}


def _wilder_smooth(series, length):
    s = pd.Series(series).astype(float)
    result = s.rolling(length).sum()
    for i in range(length, len(s)):
        if pd.isna(result.iloc[i - 1]):
            continue
        result.iloc[i] = result.iloc[i - 1] - (result.iloc[i - 1] / length) + s.iloc[i]
    return result


def _rolling_pct_rank(s, window):
    arr = s.to_numpy()
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(window - 1, n):
        w = arr[i - window + 1:i + 1]
        if np.isnan(w).any():
            continue
        out[i] = (np.sum(w <= w[-1]) - 1) / (window - 1) * 100
    return out


def _fetch_h1(total=FETCH_BARS):
    """Binance klines endpoint caps limit at 1500/call -- paginate backward."""
    MAX_PER_CALL = 1500
    frames = []
    end_time = None
    remaining = total
    while remaining > 0:
        batch_limit = min(MAX_PER_CALL, remaining)
        params = {"symbol": SYMBOL, "interval": "1h", "limit": batch_limit}
        if end_time is not None:
            params["endTime"] = end_time
        r = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        frames.append(data)
        end_time = data[0][0] - 1  # next batch ends right before this batch's first open_time
        remaining -= len(data)
        if len(data) < batch_limit:
            break
    all_rows = [row for batch in reversed(frames) for row in batch]
    df = pd.DataFrame(all_rows, columns=["open_time", "open", "high", "low", "close", "volume",
                                          "close_time", "qv", "count", "tbv", "tbqv", "ignore"])
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def _compute_trend_regime_series(df):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)

    atr_wilder = _wilder_smooth(tr, ADX_LEN)
    plus_dm_smooth = _wilder_smooth(plus_dm, ADX_LEN)
    minus_dm_smooth = _wilder_smooth(minus_dm, ADX_LEN)
    plus_di = 100 * plus_dm_smooth / atr_wilder
    minus_di = 100 * minus_dm_smooth / atr_wilder
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = _wilder_smooth(dx, ADX_LEN) / ADX_LEN

    trend_state = [None] * len(df)
    state = None
    for i in range(len(df)):
        a = adx.iloc[i]
        if pd.isna(a):
            trend_state[i] = None
            continue
        if state is None:
            state = "Trend" if a >= ADX_ENTER_TREND else "Range"
        else:
            if state == "Range" and a >= ADX_ENTER_TREND:
                state = "Trend"
            elif state == "Trend" and a < ADX_EXIT_TREND:
                state = "Range"
        trend_state[i] = state
    return trend_state, adx


def get_current_trend_regime():
    """Returns ('Trend'|'Range', last_closed_h1_bar_time, adx_value).
    Cached per H1 bar -- only refetches/recomputes when a new H1 bar has closed."""
    try:
        df = _fetch_h1()
    except Exception:
        fetched_at = _cache.get("fetched_at")
        if (
            _cache.get("regime") in {"Trend", "Range"}
            and _cache.get("h1_bar_time") is not None
            and isinstance(fetched_at, datetime)
            and datetime.now(timezone.utc) - fetched_at <= timedelta(hours=2)
        ):
            return _cache["regime"], _cache["h1_bar_time"], _cache.get("adx")
        raise
    last_closed = df.iloc[-2]  # -1 is the still-forming bar, same convention as the M15 loop
    bar_time = last_closed["ts"]

    if _cache["h1_bar_time"] == bar_time:
        return _cache["regime"], bar_time, _cache.get("adx")

    df_closed = df.iloc[:-1].reset_index(drop=True)
    trend_state, adx = _compute_trend_regime_series(df_closed)
    regime = trend_state[-1]
    _cache["h1_bar_time"] = bar_time
    _cache["regime"] = regime
    _cache["adx"] = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None
    _cache["fetched_at"] = datetime.now(timezone.utc)
    return regime, bar_time, _cache["adx"]


def _self_test():
    """ponytail: smallest check that fails if the fallback stops working."""
    global _fetch_h1
    old = dict(_cache)
    saved = _fetch_h1

    class _Boom(RuntimeError):
        pass

    def _fail(*_args, **_kwargs):
        raise _Boom("boom")

    try:
        _cache.update(
            {
                "h1_bar_time": pd.Timestamp("2026-01-01T12:00:00Z"),
                "regime": "Range",
                "adx": 23.5,
                "fetched_at": datetime.now(timezone.utc),
            }
        )
        _fetch_h1 = _fail
        regime, bar_time, adx = get_current_trend_regime()
        assert regime == "Range"
        assert str(bar_time) == "2026-01-01 12:00:00+00:00"
        assert adx == 23.5
    finally:
        _fetch_h1 = saved
        _cache.clear()
        _cache.update(old)


if __name__ == "__main__":
    _self_test()
    regime, bar_time, adx = get_current_trend_regime()
    print(f"current trend_regime: {regime}  (H1 bar: {bar_time}, ADX={adx})")
