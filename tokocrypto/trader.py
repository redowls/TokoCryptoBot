"""Trading entrypoint, every 15 minutes (cron :06/:21/:36/:51, after the
snapshot at :02/:17/:32/:47).

Order of operations: load snapshot → reconcile with Tokocrypto balances →
exits → circuit breaker → entries → persist ledger + notify. `--dry-run` logs
every decision but places no orders and mutates nothing.

Two things differ from the hourly sibling this is ported from:

  * Exits pass TWO timeframe blocks to strategy.check_exit — the 15m block for
    price and the 1H block for ATR. Price on the fast bar is the whole point of
    the faster cycle; ATR on the slow bar is what keeps 1R the width the
    strategy was validated at.
  * `extras` are read out of the snapshot rather than refetched. The snapshot
    already carries prev_close per timeframe, and at four cycles an hour the
    old two-calls-per-symbol approach would have been 80 API calls an hour for
    data already on disk.

⚠️ Tokocrypto has no verified sandbox: with TRADING_ENABLED=true this spends
real funds. Preview with `python -m tokocrypto.trader --dry-run` first.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

from . import (broker, config, ledger, lock, notify, policy, risk, strategy,
               symbols)


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def load_current_snapshot(now=None):
    """Latest snapshot no older than SNAPSHOT_MAX_AGE_MIN, else None.

    At a 15-minute cadence a stale snapshot is far more dangerous than it was
    hourly, so the budget is 20 minutes: one missed capture halts trading on the
    next cycle instead of letting the bot act on hour-old indicators.
    """
    now = now or datetime.now(timezone.utc)
    for day in (now, now - timedelta(days=1)):
        date_dir = config.DATA_DIR / day.strftime("%Y-%m-%d")
        if not date_dir.is_dir():
            continue
        for path in sorted(date_dir.glob("*-*.json"), reverse=True):
            try:
                snap = json.loads(path.read_text())
                captured = datetime.fromisoformat(snap["captured_at"])
            except (OSError, ValueError, KeyError):
                continue
            if now - captured <= timedelta(minutes=config.SNAPSHOT_MAX_AGE_MIN):
                return snap
            return None  # newest is already stale; older ones cannot be fresher
    return None


def _coin(snap, symbol):
    for coin in snap.get("symbols", []):
        if coin.get("symbol") == symbol:
            return coin
    return None


def _block(snap, symbol, tf_key):
    """One timeframe block for one symbol, or None if it is not usable."""
    coin = _coin(snap, symbol)
    if not coin:
        return None
    tf = (coin.get("timeframes") or {}).get(tf_key) or {}
    return tf if tf.get("status") == "ok" else None


def prices_from_snapshot(snap):
    """{symbol: last signal-timeframe close} — the marks used to value balances.

    The 15m close is the freshest mark available and is what an exit would
    realistically fill near.
    """
    out = {}
    for coin in snap.get("symbols", []):
        tf = (coin.get("timeframes") or {}).get(config.SIGNAL_TF) or {}
        if tf.get("status") == "ok" and tf.get("last_close"):
            out[coin["symbol"]] = tf["last_close"]
    return out


def extras_from_snapshot(snap):
    """Fields strategy.evaluate_entry needs that are not indicators.

    Derived entirely from the snapshot — no network. `day_change_pct` comes off
    the daily bar's previous close, the 1H pair off the hourly bar's.
    """
    out = {}
    for coin in snap.get("symbols", []):
        sym = coin.get("symbol")
        tfs = coin.get("timeframes") or {}
        d1 = tfs.get(config.REGIME_TF) or {}
        h1 = tfs.get(config.QUALIFY_TF) or {}
        extras = {"day_change_pct": None, "last_1h_close": None, "prev_1h_close": None}
        if d1.get("status") == "ok" and d1.get("prev_close"):
            extras["day_change_pct"] = (d1["last_close"] / d1["prev_close"] - 1) * 100
        if h1.get("status") == "ok":
            extras["last_1h_close"] = h1.get("last_close")
            extras["prev_1h_close"] = h1.get("prev_close")
        out[sym] = extras
    return out


def check_one_exit(snap, pos, reg, hours_held):
    """Evaluate one position. Price comes from 15m, ATR from 1H."""
    signal = _block(snap, pos["symbol"], config.SIGNAL_TF)
    anchor = _block(snap, pos["symbol"], config.ATR_ANCHOR_TF)
    if not signal or not anchor:
        return None, pos
    return strategy.check_exit(pos, signal, anchor, reg, hours_held)


def _order_outcome(order):
    """(status, fill_price, filled_qty) out of an MBX order dict."""
    status = order.get("status")
    filled = float(order.get("executedQty") or 0)
    price = float(order.get("executedPrice") or 0) or None
    return status, price, filled


def _sellable_qty(pos, positions_by_sym):
    """Quantity we may actually sell: the free balance, floored to step size.

    Fees are taken in-asset, so the held quantity drifts a little below what the
    entry fill reported; selling the ledger's number would be rejected.
    """
    sym = pos["symbol"]
    live = positions_by_sym.get(sym) or {}
    qty = min(live.get("free", pos["qty"]), pos["qty"]) if live else pos["qty"]
    return symbols.round_qty(config.pair(sym), qty)


def _exit_position(led, pos, price_hint, reason, dry_run, positions_by_sym):
    sym = pos["symbol"]
    pair = config.pair(sym)
    qty = _sellable_qty(pos, positions_by_sym)
    if dry_run:
        log(f"DRY-RUN exit {sym}: {reason} qty {qty} @ ~{config.fmt_price(price_hint)}")
        return None
    if qty <= 0:
        log(f"exit {sym}: {reason} but no sellable balance — dropping from ledger")
        return ledger.close_position(led, pos, price_hint, f"{reason}/no-balance")
    ok, why = symbols.meets_minimums(pair, qty, price_hint)
    if not ok:
        log(f"exit {sym}: {reason} but {why} — cannot sell, dropping from ledger")
        return ledger.close_position(led, pos, price_hint, f"{reason}/below-minimum")
    exit_price = price_hint
    try:
        order_id = broker.close_position(pair, qty)
        status, fill_price, filled_qty = _order_outcome(broker.wait_for_fill(pair, order_id))
        if fill_price:
            exit_price = fill_price
        log(f"exit {sym}: {reason}, sell order status {status} qty {filled_qty} "
            f"@ {config.fmt_price(exit_price)}")
        if not filled_qty:
            log(f"exit {sym}: sell did not fill — keeping position, will retry next cycle")
            return None
    except broker.BrokerError as e:
        log(f"exit {sym}: sell FAILED ({e}) — keeping position, will retry next cycle")
        notify.send(f"TokoCryptoBot EXIT FAILED {sym} ({reason}): {e}")
        return None
    # Real commission (plus tax) if the exchange will tell us; the ledger falls
    # back to a modelled cost and flags the trade estimated when it will not.
    fill = broker.fill_summary(pair, order_id)
    if fill["price"]:
        exit_price = fill["price"]
    trade = ledger.close_position(led, pos, exit_price, reason,
                                  exit_fee=fill["commission"] if fill["fills"] else None)
    notify.send(f"TokoCryptoBot EXIT {sym} ({reason}) @ {config.fmt_price(exit_price)} "
                f"P&L {config.fmt_usdt(trade['pnl'])} net "
                f"(fees {config.fmt_usdt(trade['fees'])})")
    return trade


def _enter_position(led, sym, coin, equity, reg, dry_run):
    """Size on the 1H bar, price on the 15m bar.

    The ATR handed to risk.position_size MUST be the 1H one — it is what 1R
    means, and the fee-drag ceiling is expressed as a fraction of 1R.
    """
    pair = config.pair(sym)
    anchor = (coin["timeframes"] or {}).get(config.ATR_ANCHOR_TF) or {}
    signal = (coin["timeframes"] or {}).get(config.SIGNAL_TF) or {}
    atr = anchor.get("atr14")
    price = signal.get("last_close") or anchor.get("last_close")
    if not atr or not price:
        log(f"entry {sym}: missing {config.ATR_ANCHOR_TF} ATR or price — skipped")
        return None
    qty, stop, risk_usdt = risk.position_size(equity, price, atr,
                                              half=(reg == "risk_off"), sym_pair=pair)
    if qty <= 0:
        log(f"entry {sym}: unsizable — {risk.sizing_reason(equity, price, atr, pair)}")
        return None
    notional = round(qty * price, 2)
    if dry_run:
        log(f"DRY-RUN entry {sym}: qty {qty} @ ~{config.fmt_price(price)} "
            f"(notional {config.fmt_usdt(notional)}), stop {config.fmt_price(stop)}, "
            f"risk {config.fmt_usdt(risk_usdt)}")
        return None
    ledger.record_entry_attempt(led, sym)
    try:
        # A market BUY on Tokocrypto is sized in USDT, not coin.
        order_id = broker.market_buy_quote(pair, notional)
        status, fill_price, filled_qty = _order_outcome(broker.wait_for_fill(pair, order_id))
    except broker.BrokerError as e:
        log(f"entry {sym}: order FAILED ({e})")
        notify.send(f"TokoCryptoBot ENTRY FAILED {sym}: {e}")
        return None
    if not filled_qty:
        log(f"entry {sym}: order status {status} unfilled — skipped")
        return None
    fill = broker.fill_summary(pair, order_id)
    entry_price = fill["price"] or fill_price or price
    pos = ledger.open_position(led, sym, filled_qty, entry_price, atr, order_id,
                               half_size=(reg == "risk_off"),
                               entry_fee=fill["commission"] if fill["fills"] else None)
    log(f"entry {sym}: status {status} qty {filled_qty} @ {config.fmt_price(entry_price)}, "
        f"stop {config.fmt_price(pos['stop'])}")
    notify.send(f"TokoCryptoBot ENTRY {sym} qty {filled_qty} @ {config.fmt_price(entry_price)} "
                f"stop {config.fmt_price(pos['stop'])} (regime {reg})")
    return pos


def run(dry_run=False, now=None):
    """One 15-minute cycle, holding the order lock.

    Waits rather than skips: a skipped cycle suspends exits, trailing,
    reconciliation and the circuit breaker until the next one.
    """
    now = now or datetime.now(timezone.utc)
    if not dry_run and not config.TRADING_ENABLED:
        log("TRADING_ENABLED is false — exiting (use --dry-run to preview decisions)")
        return
    if dry_run:
        return _run(dry_run, now)
    with lock.held(wait_s=120) as acquired:
        if not acquired:
            log("order lock still busy after 120s — skipping this cycle")
            return
        return _run(dry_run, now)


def _run(dry_run=False, now=None):
    now = now or datetime.now(timezone.utc)
    snap = load_current_snapshot(now)
    if snap is None:
        log("no fresh snapshot (missing or older than "
            f"{config.SNAPSHOT_MAX_AGE_MIN} min) — skipping cycle")
        return

    marks = prices_from_snapshot(snap)
    have_keys = bool(config.TOKO_KEY and config.TOKO_SECRET)
    positions = []
    if have_keys:
        try:
            acct = broker.get_account(price_by_symbol=marks)
        except broker.BrokerError as e:
            log(f"cannot reach Tokocrypto ({e}) — skipping cycle")
            return
        positions = acct["positions"]
        equity = acct["equity"]
        log(f"account equity {config.fmt_usdt(equity)} "
            f"(cash {config.fmt_usdt(acct['cash'])}), {len(positions)} coin positions")
    elif dry_run:
        equity, positions = config.DRY_RUN_EQUITY_USDT, None
        log(f"no Tokocrypto keys — dry-run with simulated equity "
            f"{config.fmt_usdt(equity)}")
    else:
        log("no Tokocrypto keys — cannot trade")
        return

    led = ledger.load()
    positions_by_sym = {p["symbol"]: p for p in (positions or [])}
    if positions is not None:
        for note in ledger.reconcile(led, positions):
            log(note)

    pol = policy.load(now=now)
    computed = strategy.regime(snap)
    reg = strategy.effective_regime(computed, pol["regime_hint"])
    log(f"regime: computed {computed}, policy hint {pol['regime_hint']} -> {reg}")

    # exits first
    for pos in list(led["open"]):
        signal = _block(snap, pos["symbol"], config.SIGNAL_TF)
        if signal is None:
            log(f"exit check {pos['symbol']}: no {config.SIGNAL_TF} data this cycle — holding")
            continue
        action, updated = check_one_exit(snap, pos, reg, ledger.hours_held(pos, now))
        if action:
            _exit_position(led, updated, signal["last_close"], action, dry_run,
                           positions_by_sym)
        else:
            ledger.update_position(led, updated)
            if updated["stop"] != pos["stop"]:
                log(f"trail {pos['symbol']}: stop -> {config.fmt_price(updated['stop'])}")
            if updated.get("lock") != pos.get("lock"):
                log(f"lock {pos['symbol']}: profit lock -> "
                    f"{config.fmt_price(updated['lock'])} "
                    f"(peak {config.fmt_price(updated['high_water'])})")

    # An unfunded account is not a risk event — say so plainly rather than
    # letting the circuit breaker (which treats equity<=0 as tripped) claim a
    # 24h loss that never happened.
    if equity <= 0:
        log("account has no equity — deposit USDT before trading; nothing to do")
        if not dry_run:
            ledger.save(led)
        return

    if risk.circuit_breaker_tripped(led["closed"], equity, now=now):
        log(f"circuit breaker: 24h realized loss >= {config.CIRCUIT_BREAKER_PCT:.0%} "
            "of equity — no new entries")
        if not dry_run:
            ledger.save(led)
        return

    # entries
    open_syms = {p["symbol"] for p in led["open"]}
    slots = min(pol["max_positions"], config.MAX_POSITIONS) - len(open_syms)
    if slots <= 0:
        log(f"no entry slots ({len(open_syms)} open)")
    else:
        extras = extras_from_snapshot(snap)
        candidates, rejections = strategy.entry_candidates(
            snap, extras, open_syms, reg, blocked=set(pol["blocked_symbols"]))
        for sym, reason in rejections:
            log(f"reject {sym}: {reason}")
        entered = 0
        for sym, coin in candidates:
            if entered >= slots:
                break
            if ledger.throttled(led, sym, now=now):
                log(f"reject {sym}: re-entry throttle "
                    f"({config.REENTRY_THROTTLE_HOURS}h)")
                continue
            if _enter_position(led, sym, coin, equity, reg, dry_run) or dry_run:
                entered += 1
        if not candidates:
            log("no entry candidates this cycle")

    if not dry_run:
        ledger.save(led)
    log("cycle done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TokoCryptoBot 15-minute trader")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions without placing orders or saving state")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
