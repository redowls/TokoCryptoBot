"""Position sizing and account-level risk controls.

Sizing is the same Van-Tharp-style rule CryptoAutoBot used — risk RISK_PCT of
equity against a STOP_ATR_MULT*ATR stop — with two exchange-specific additions:
the quantity is floored to the symbol's LOT_SIZE step, and an order that would
fall under the symbol's NOTIONAL minimum is refused rather than sent to be
rejected by the exchange.

The ATR passed in MUST come from the 1H timeframe (config.ATR_ANCHOR_TF), not
the 15m signal bar. Stop distance is what 1R means, and fee drag is measured as
a fraction of 1R — sizing off a faster bar silently triples the drag.

All money amounts are USDT.
"""
from datetime import datetime, timedelta, timezone

from . import config, symbols


def position_size(equity, entry_price, atr, half=False, sym_pair=None):
    """Risk RISK_PCT of equity with a STOP_ATR_MULT*ATR stop.

    Returns (qty, initial_stop, risk_usdt) or (0, None, 0) if unsizable.
    """
    if not atr or atr <= 0 or not entry_price or entry_price <= 0 or equity <= 0:
        return 0.0, None, 0.0
    stop_dist = config.STOP_ATR_MULT * atr
    if stop_dist >= entry_price:
        return 0.0, None, 0.0  # stop below zero — volatility too wide to size
    risk_usdt = equity * config.RISK_PCT
    if half:
        risk_usdt /= 2
    qty = risk_usdt / stop_dist
    # never exceed the cash a single position may use (cap notional at 1/MAX_POSITIONS)
    max_notional = equity / config.MAX_POSITIONS
    if qty * entry_price > max_notional:
        qty = max_notional / entry_price
    if sym_pair:
        qty = symbols.round_qty(sym_pair, qty)
        ok, _ = symbols.meets_minimums(sym_pair, qty, entry_price)
        if not ok:
            return 0.0, None, 0.0
    else:
        qty = round(qty, 8)
    if qty <= 0:
        return 0.0, None, 0.0
    return qty, entry_price - stop_dist, risk_usdt


def sizing_reason(equity, entry_price, atr, sym_pair=None):
    """Human-readable explanation for a refused size (logging only)."""
    if not atr or atr <= 0:
        return "no ATR"
    if not entry_price or entry_price <= 0:
        return "no price"
    if equity <= 0:
        return "no equity"
    if config.STOP_ATR_MULT * atr >= entry_price:
        return "stop distance exceeds price"
    if sym_pair:
        qty = symbols.round_qty(sym_pair, (equity * config.RISK_PCT) / (config.STOP_ATR_MULT * atr))
        ok, why = symbols.meets_minimums(sym_pair, qty, entry_price)
        if not ok:
            return why
    return "unsizable"


def circuit_breaker_tripped(closed_trades, equity, now=None):
    """True when realized losses over the trailing 24h reach CIRCUIT_BREAKER_PCT."""
    if equity <= 0:
        return True
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    realized = 0.0
    for t in closed_trades:
        try:
            exit_time = datetime.fromisoformat(t["exit_time"])
        except (KeyError, ValueError):
            continue
        if exit_time >= cutoff:
            realized += t.get("pnl", 0.0)
    return realized <= -config.CIRCUIT_BREAKER_PCT * equity
