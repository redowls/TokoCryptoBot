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


MAX_PAGE = 1000          # exchange hard cap per request
MAX_PAGES = 400          # ~400k 15m bars; a stop so a bad `end` cannot spin
PAGE_PAUSE_S = 0.12      # IP rate limits are weight-based; stay well under
MAX_RETRIES = 4
RETRY_BASE_S = 1.0       # 1s, 2s, 4s, 8s


def _is_retryable(exc):
    """5xx and transport errors are worth repeating; 4xx are not.

    A 400 or 404 means the request itself is wrong, so retrying only burns
    rate-limit budget.

    429 and 418 are NOT the same answer. 429 is "you are going too fast" — back
    off and obey Retry-After. 418 is "you are already banned", for 2 minutes up
    to 3 days, escalating for repeat offenders. No amount of backing off inside
    a run that lasts minutes outlives a ban that may last days, and continuing
    to knock is precisely what lengthens it. So a 418 aborts the run and lets a
    human see it, rather than quietly digging the hole deeper.
    """
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        # Only genuine transport faults. A bare TypeError or AttributeError is
        # a bug in this file, and retrying it four times with backoff turns a
        # fast failure into a slow one while fixing nothing.
        return isinstance(exc, (requests.ConnectionError, requests.Timeout))
    if status == 418:
        return False                      # already banned — see below
    if status == 429:
        return True                       # rate-limited: back off, obey Retry-After
    return status >= 500


def _retry_after_s(exc, attempt):
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status in (429, 418):
        try:
            return max(float((resp.headers or {}).get("Retry-After", 0)), 1.0)
        except (TypeError, ValueError):
            return 5.0
    return RETRY_BASE_S * (2 ** attempt)


def fetch_klines(chart_symbol, interval, limit=500, start=None, end=None,
                 closed_only=True, now_ms=None, session=None):
    """OHLCV bars for one chart symbol ("BTCUSDT") and interval ("15m").

    Returns a list, oldest first, possibly empty. `start`/`end` are unix
    milliseconds. With `closed_only`, any trailing bar whose close time has not
    yet passed is dropped.

    The exchange caps a response at 1000 bars and answers a wide window with
    the OLDEST 1000 of it. Requesting 90 days therefore used to return ten days
    from three months ago and look like a 90-day sample. When the window needs
    more than one page this walks `startTime` forward until the window is
    covered or the feed runs dry, so `days` means days.
    """
    if start is not None and end is not None:
        return _fetch_paged(chart_symbol, interval, int(start), int(end),
                            closed_only, now_ms, session)
    return _fetch_page(chart_symbol, interval, limit, start, end,
                       closed_only, now_ms, session)


def _fetch_paged(chart_symbol, interval, start, end, closed_only, now_ms, session):
    out, seen, cursor = [], set(), start
    for _ in range(MAX_PAGES):
        page = _fetch_page(chart_symbol, interval, MAX_PAGE, cursor, end,
                           closed_only, now_ms, session)
        fresh = [b for b in page if b["t"] not in seen]
        if not fresh:
            break                      # dry feed, or the page repeated its edge
        seen.update(b["t"] for b in fresh)
        out.extend(fresh)
        if len(page) < MAX_PAGE:
            break                      # short page means the feed is exhausted
        # Resume from the last bar's open time; the dedupe above drops the
        # overlap, which is safer than guessing the interval's millisecond step.
        nxt = _open_ms(page[-1])
        if nxt is None or nxt <= cursor:
            break                      # no forward progress — stop rather than spin
        cursor = nxt
        if PAGE_PAUSE_S:
            time.sleep(PAGE_PAUSE_S)
    out.sort(key=lambda b: b["t"])
    return out


def _open_ms(bar):
    try:
        return int(datetime.fromisoformat(bar["t"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def _fetch_page(chart_symbol, interval, limit=500, start=None, end=None,
                closed_only=True, now_ms=None, session=None):
    label = f"{chart_symbol} {interval}"
    params = {"symbol": chart_symbol, "interval": interval, "limit": limit}
    if start is not None:
        params["startTime"] = int(start)
    if end is not None:
        params["endTime"] = int(end)
    getter = (session or requests).get
    payload = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = getter(f"{config.KLINE_BASE_URL}{config.KLINES_PATH}", params=params,
                       headers={"User-Agent": config.USER_AGENT}, timeout=20)
            r.raise_for_status()
            payload = r.json()
            break
        except FetchError:
            raise
        except Exception as e:  # network, HTTP, JSON
            if attempt >= MAX_RETRIES or not _is_retryable(e):
                raise FetchError(f"{label}: {e}") from e
            time.sleep(_retry_after_s(e, attempt))

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
