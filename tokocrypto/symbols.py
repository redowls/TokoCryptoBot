"""Symbol metadata: order minimums and quantity precision.

Tokocrypto nests constraints in a `filters` array, Binance-style. The two that
bind us:

  * LOT_SIZE.stepSize — quantity must be a multiple of it. Always round DOWN;
    rounding up produces an order we cannot fund.
  * NOTIONAL.minNotional — price * quantity floor, $5 on most USDT pairs
    ($1 on DOGE).

Type-3 ("Nextme") symbols are dropped: they are served by a different kline
host and carry no ticker data, so the pipeline cannot price them.
"""
import json
import math
import time

import requests

from . import config, net

CACHE_MAX_AGE_S = 24 * 3600
_MEM = {"data": None}

if config.FORCE_IPV4:
    net.force_ipv4()


def _fetch(session=None):
    getter = (session or requests).get
    r = getter(f"{config.OPEN_BASE_URL}{config.SYMBOLS_PATH}",
               headers={"User-Agent": config.USER_AGENT}, timeout=20)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("list") or []


def _read_cache():
    try:
        raw = json.loads(config.SYMBOLS_CACHE.read_text())
        if time.time() - raw["at"] < CACHE_MAX_AGE_S:
            return raw["data"]
    except Exception:
        return None
    return None


def _write_cache(data):
    try:
        config.SYMBOLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        config.SYMBOLS_CACHE.write_text(json.dumps({"at": time.time(), "data": data}))
    except Exception:
        pass  # a cache we cannot write is a slow path, not a failure


def load(force=False, now=None):
    """Symbol metadata keyed by pair ("BTC_USDT"). Cached on disk for 24h."""
    if not force and _MEM["data"] is not None:
        return _MEM["data"]
    data = None if force else _read_cache()
    if data is None:
        data = {s["symbol"]: s for s in _fetch() if s.get("type") == 1}
        _write_cache(data)
    _MEM["data"] = data
    return data


def meta(sym_pair):
    return load().get(sym_pair) or {}


def _filter(sym_pair, kind):
    for f in meta(sym_pair).get("filters") or []:
        if f.get("filterType") == kind:
            return f
    return {}


def min_notional(sym_pair):
    try:
        return float(_filter(sym_pair, "NOTIONAL")["minNotional"])
    except (KeyError, TypeError, ValueError):
        return config.MIN_ORDER_USDT


def step_size(sym_pair):
    try:
        return float(_filter(sym_pair, "LOT_SIZE")["stepSize"])
    except (KeyError, TypeError, ValueError):
        return 1e-8


def min_qty(sym_pair):
    try:
        return float(_filter(sym_pair, "LOT_SIZE")["minQty"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def round_qty(sym_pair, qty):
    """Floor to stepSize.

    Never rounds up. Fees are taken in-asset so the held quantity drifts below
    the fill, and an order rounded up is one the balance cannot cover.
    """
    step = step_size(sym_pair)
    if step <= 0:
        return float(qty)
    # Round the quotient before flooring: 0.00012/0.00001 lands on 11.999...
    # in binary float, which would floor to 11 and silently shrink the order.
    return math.floor(round(float(qty) / step, 9)) * step


def meets_minimums(sym_pair, qty, price):
    """(ok, reason). The reason is what risk.sizing_reason reports to the log."""
    if qty <= 0:
        return False, "qty rounds to zero"
    if price <= 0:
        return False, "no price"
    floor_qty = min_qty(sym_pair)
    if qty < floor_qty:
        return False, f"qty {qty:g} below minQty {floor_qty:g}"
    floor = max(min_notional(sym_pair), config.MIN_ORDER_USDT)
    notional = qty * price
    if notional < floor:
        return False, (f"notional {config.fmt_usdt(notional)} below minimum "
                       f"{config.fmt_usdt(floor)}")
    return True, "ok"
