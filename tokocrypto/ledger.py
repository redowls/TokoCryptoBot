"""Trade ledger: data/trades/trades.json.

Tokocrypto spot balances are the source of truth for *what* is held, but a balance
carries no entry price, stop or open-time — so unlike the Alpaca original this
ledger is the only record of position context. Losing it means the bot can no
longer manage an open position (it will re-adopt the balance with a synthetic
stop). It also holds closed-trade history and the per-coin re-entry throttle.
"""
import json
from datetime import datetime, timedelta, timezone

from . import config

EMPTY = {"open": [], "closed": [], "last_entry_attempt": {}}


def load(path=None):
    path = path or config.TRADES_DIR / "trades.json"
    try:
        led = json.loads(path.read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(EMPTY))
    for key, default in EMPTY.items():
        led.setdefault(key, json.loads(json.dumps(default)))
    return led


def save(led, path=None):
    path = path or config.TRADES_DIR / "trades.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(led, indent=2))


def _modelled_fee(sym_pair, notional):
    """Cost to assume when the exchange did not report one for this fill.

    Commission PLUS Indonesian withholding. Tokocrypto reports `commission` and
    `taxAmount` as separate fields on every fill and both are money leaving the
    account, so both belong in the estimate. Modelling only the commission is
    how CryptoAutoBot reported gross P&L as net for months (IMP-B01: a $463 gap,
    0.206% per side, 22% of gross profit) — the omission is invisible until a
    broker reconciliation catches it.

    A realised fee reported by the exchange always overrides this.
    """
    return abs(float(notional)) * (config.TAKER_FEE_PCT + config.TAX_PCT)


def open_position(led, symbol, qty, entry_price, atr, order_id, half_size=False, now=None,
                  entry_fee=None):
    now = now or datetime.now(timezone.utc)
    stop = entry_price - config.STOP_ATR_MULT * atr
    fee = _modelled_fee(config.pair(symbol), qty * entry_price) if entry_fee is None else float(entry_fee)
    pos = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": now.isoformat(),
        "initial_stop": stop,
        "stop": stop,
        "high_water": entry_price,
        "atr_at_entry": atr,
        "order_id": order_id,
        "half_size": half_size,
        "entry_fee": round(fee, 6),
        "entry_fee_estimated": entry_fee is None,
    }
    led["open"].append(pos)
    return pos


def close_position(led, pos, exit_price, reason, now=None, exit_fee=None):
    """Book a closed trade with `pnl` NET of both commissions.

    `pnl` used to be gross — `(exit - entry) * qty` with no fee term anywhere —
    which is how the ledger came to claim +Rp22.260 realised on an account that
    was down Rp7.035 in its Indodax predecessor. Every consumer reads `pnl` (the digest, the circuit
    breaker, the daily routine that writes policy.json), so the headline field
    is the one that has to be honest; the gross figure stays alongside it rather
    than disappearing.

    Fees are the real commissions the exchange reported when the trader could
    fetch them, and a modelled taker fee otherwise. `fees_estimated` says which,
    so a backfilled or degraded number is never mistaken for a measured one.
    """
    now = now or datetime.now(timezone.utc)
    led["open"] = [p for p in led["open"] if p is not pos and p["symbol"] != pos["symbol"]]
    qty = pos["qty"]
    entry_fee = float(pos.get("entry_fee") or _modelled_fee(config.pair(pos["symbol"]), qty * pos["entry_price"]))
    x_fee = _modelled_fee(config.pair(pos["symbol"]), qty * exit_price) if exit_fee is None else float(exit_fee)
    gross = (exit_price - pos["entry_price"]) * qty
    fees = entry_fee + x_fee
    # R geometry travels with the trade so the doctrine buckets (scorecard.py)
    # and the giveback view (replay.py) never have to guess it later.
    r = (pos["entry_price"] - pos["initial_stop"]) if pos.get("initial_stop") else None
    peak = pos.get("high_water")
    trade = {
        "symbol": pos["symbol"],
        "qty": qty,
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "entry_time": pos["entry_time"],
        "exit_time": now.isoformat(),
        "pnl": round(gross - fees, 2),
        "pnl_gross": round(gross, 2),
        "fees": round(fees, 2),
        "fees_estimated": bool(pos.get("entry_fee_estimated", True)) or exit_fee is None,
        "order_id": pos.get("order_id"),
        "reason": reason,
        "initial_stop": pos.get("initial_stop"),
        "peak_price": peak,
        "r_multiple": round((exit_price - pos["entry_price"]) / r, 4) if r else None,
        "peak_r": round((peak - pos["entry_price"]) / r, 4) if (r and peak) else None,
    }
    led["closed"].append(trade)
    return trade


def update_position(led, pos):
    for i, p in enumerate(led["open"]):
        if p["symbol"] == pos["symbol"]:
            led["open"][i] = pos
            return


def hours_held(pos, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 3600


def throttled(led, symbol, now=None):
    """True while the per-coin re-entry throttle window is active."""
    now = now or datetime.now(timezone.utc)
    last = led["last_entry_attempt"].get(symbol)
    if not last:
        return False
    return now - datetime.fromisoformat(last) < timedelta(hours=config.REENTRY_THROTTLE_HOURS)


def record_entry_attempt(led, symbol, now=None):
    now = now or datetime.now(timezone.utc)
    led["last_entry_attempt"][symbol] = now.isoformat()


def reconcile(led, exchange_positions, now=None):
    """Sync ledger open positions with the exchange balances. Returns log lines.

    - ledger position with no balance on Tokocrypto → drop it (sold outside the bot)
    - balance unknown to the ledger → adopt with a synthetic stop so exits manage it
    - qty mismatch → the exchange wins (fees are deducted in-asset, so the held
      quantity drifts slightly below what the fill reported)
    """
    now = now or datetime.now(timezone.utc)
    notes = []
    by_sym = {p["symbol"]: p for p in exchange_positions}
    kept = []
    for pos in led["open"]:
        live = by_sym.pop(pos["symbol"], None)
        if live is None:
            notes.append(f"reconcile: {pos['symbol']} no balance on Tokocrypto — dropped from ledger")
            continue
        if abs(live["qty"] - pos["qty"]) > 1e-9:
            notes.append(f"reconcile: {pos['symbol']} qty {pos['qty']} -> {live['qty']} (exchange wins)")
            pos["qty"] = live["qty"]
        kept.append(pos)
    for sym, live in by_sym.items():
        price = live.get("current_price") or live.get("avg_entry_price")
        if not price:
            notes.append(f"reconcile: {sym} balance held but no price — not adopted")
            continue
        stop = price * 0.97 if not live.get("atr") else price - config.STOP_ATR_MULT * live["atr"]
        kept.append({
            "symbol": sym,
            "qty": live["qty"],
            "entry_price": live.get("avg_entry_price", price),
            "entry_time": now.isoformat(),
            "initial_stop": stop,
            "stop": stop,
            "high_water": price,
            "atr_at_entry": live.get("atr"),
            "order_id": None,
            "half_size": False,
            "adopted": True,
        })
        notes.append(f"reconcile: adopted unknown Tokocrypto balance {sym} qty {live['qty']}")
    led["open"] = kept
    return notes
