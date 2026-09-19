"""Tokocrypto public market data.

Klines come back as arrays, not objects, wrapped in a {"code","data"} envelope:

    [openTime, open, high, low, close, volume, closeTime, ...]

with millisecond timestamps and numeric fields as strings. `fetch_klines`
normalises them to the {"t","o","h","l","c","v"} shape the rest of the pipeline
speaks, so indicators/snapshot/strategy port from CryptoIndodaxBot untouched.
That normalisation is the same trick that carried CryptoAutoBot from Alpaca to
Indodax; it is what keeps the strategy layer exchange-agnostic.

The last row is the *currently forming* candle. On a 15-minute cycle, letting a
partial bar into the indicators means every EMA and ADX flickers inside the bar
and the bot trades its own noise — the most likely source of phantom signals in
this design. `closed_only` drops it by default.
"""
import time
from datetime import datetime, timezone

import requests

from . import config, net


class FetchError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()


def normalize_kline(row):
    """One Tokocrypto kline array -> an Alpaca-shaped bar dict."""
    return {
        "t": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat(),
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]),
    }


def _close_time(row):
    try:
        return int(row[6])
    except (IndexError, TypeError, ValueError):
        return None


def _rows(payload, label):
    """Unwrap {"code":0,"data":[...]} or accept a bare list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("code") not in (0, None):
            raise FetchError(f"{label}: {payload.get('msg') or payload.get('message')} "
                             f"(code {payload.get('code')})")
        rows = payload.get("data")
        if isinstance(rows, list):
            return rows
    raise FetchError(f"{label}: unexpected payload {payload!r:.120}")


def fetch_klines(chart_symbol, interval, limit=500, start=None, end=None,
                 closed_only=True, now_ms=None, session=None):
    """OHLCV bars for one chart symbol ("BTCUSDT") and interval ("15m").

    Returns a list, oldest first, possibly empty. `start`/`end` are unix
    milliseconds. With `closed_only`, any trailing bar whose close time has not
    yet passed is dropped.
    """
    label = f"{chart_symbol} {interval}"
    params = {"symbol": chart_symbol, "interval": interval, "limit": limit}
    if start is not None:
        params["startTime"] = int(start)
    if end is not None:
        params["endTime"] = int(end)
    getter = (session or requests).get
    try:
        r = getter(f"{config.KLINE_BASE_URL}{config.KLINES_PATH}", params=params,
                   headers={"User-Agent": config.USER_AGENT}, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except FetchError:
        raise
    except Exception as e:  # network, HTTP, JSON
        raise FetchError(f"{label}: {e}") from e

    rows = _rows(payload, label)
    if closed_only and rows:
        cutoff = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = [row for row in rows
                if _close_time(row) is not None and _close_time(row) <= cutoff]

    out = []
    for row in rows:
        try:
            out.append(normalize_kline(row))
        except (IndexError, KeyError, TypeError, ValueError):
            continue  # skip a malformed bar rather than lose the whole series
    return out
