"""Deterministic trading rules distilled from memory/insights.md.

Pure functions over snapshot dicts (see snapshot.py output shape). The trader
supplies `extras` per symbol for fields the snapshot does not store:
{"day_change_pct": float, "last_1h_close": float, "prev_1h_close": float}.
"""
from . import config

SEVERITY = {"risk_on": 0, "neutral": 1, "risk_off": 2}


def _tf(coin, key):
    tf = coin.get("timeframes", {}).get(key, {})
    return tf if tf.get("status") == "ok" else None


def stack(tf):
    """EMA stack direction for one timeframe dict: UP / DOWN / MIXED."""
    e8, e20, e55 = tf.get("ema8"), tf.get("ema20"), tf.get("ema55")
    if None in (e8, e20, e55):
        return "MIXED"
    if e8 > e20 > e55:
        return "UP"
    if e8 < e20 < e55:
        return "DOWN"
    return "MIXED"


def regime(snap):
    """BTC regime gate over the daily timeframe (insights 06-18, 06-20, 07-10)."""
    btc = next((s for s in snap.get("symbols", []) if s.get("symbol") == "BTC"), None)
    if not btc:
        return "neutral"
    d1 = _tf(btc, config.REGIME_TF)
    if not d1 or d1.get("adx14") is None:
        return "neutral"
    st, adx = stack(d1), d1["adx14"]
    if st == "DOWN" and adx >= 25:
        return "risk_off"
    if st == "UP" and adx >= 20:
        return "risk_on"
    return "neutral"


def effective_regime(computed, policy_hint):
    """Policy hint may only make the regime MORE conservative, never less."""
    if policy_hint not in SEVERITY:
        return computed
    return computed if SEVERITY[computed] >= SEVERITY[policy_hint] else policy_hint


def evaluate_entry(coin, extras, reg):
    """Return (ok, reason). Applies the insight filters in order; the first
    failing filter is the reported reason (rejected-entry logging)."""
    h1 = _tf(coin, config.QUALIFY_TF)
    if not h1:
        return False, f"no {config.QUALIFY_TF} data"
    adx, rsi = h1.get("adx14"), h1.get("rsi14")
    if adx is None or rsi is None:
        return False, "indicators not warm"
    # Volatility floor first: a coin whose 1H ATR cannot pay the round trip is
    # uneconomic no matter how clean its trend looks, and reporting the fee drag
    # rather than a downstream ADX miss is what makes that visible in the log.
    atr, close = h1.get("atr14"), h1.get("last_close")
    if not atr or not close:
        return False, "no ATR"
    atr_pct = atr / close * 100.0
    if atr_pct < config.MIN_ATR_PCT:
        drag = config.OBSERVED_ROUND_TRIP_PCT / (config.STOP_ATR_MULT * atr_pct)
        return False, (f"fee drag {drag:.0%} of 1R "
                       f"(ATR {atr_pct:.2f}% < {config.MIN_ATR_PCT:.2f}%)")
    adx_min = config.ENTRY_ADX_MIN if reg == "risk_on" else config.ENTRY_ADX_MIN_CAUTIOUS
    if adx < adx_min:
        return False, f"ADX {adx:.1f} < {adx_min:.0f}"
    if stack(h1) != "UP":
        return False, f"{config.QUALIFY_TF} stack {stack(h1)}"
    h4 = _tf(coin, config.VETO_TF)
    if h4 and stack(h4) == "DOWN":
        return False, f"{config.VETO_TF} stack DOWN"
    if rsi > config.BLOWOFF_RSI:
        return False, f"blow-off RSI {rsi:.1f}"
    if not (config.ENTRY_RSI_MIN <= rsi <= config.ENTRY_RSI_MAX):
        return False, f"RSI {rsi:.1f} outside [{config.ENTRY_RSI_MIN:.0f},{config.ENTRY_RSI_MAX:.0f}]"
    day_pct = extras.get("day_change_pct")
    if day_pct is None:
        return False, "no day-change data"
    if day_pct > config.LATE_ENTRY_DAY_PCT:
        return False, f"late entry +{day_pct:.1f}% on day"
    if reg == "risk_off":
        if day_pct <= 0:
            return False, "risk_off: not green on day"
        if rsi > 65:
            return False, f"risk_off: RSI {rsi:.1f} > 65"
    last, prev = extras.get("last_1h_close"), extras.get("prev_1h_close")
    if last is None or prev is None:
        return False, "no 1H close history"
    if last <= prev:
        return False, "1H close not green"

    # --- timing layer ----------------------------------------------------
    # Everything above decides WHETHER this coin is tradeable; these two decide
    # WHEN. They are checked last on purpose: at four cycles an hour the
    # rejection log is the main window into the bot's behaviour, and
    # "eligible, not yet triggered" has to stay distinguishable from
    # "not eligible". Reporting a 15m miss on a coin that also failed RSI would
    # hide the real funnel.
    m30 = _tf(coin, config.CONFIRM_TF)
    if m30 and stack(m30) == "DOWN":
        # Confirmation only — a MIXED 30m is indecision, not opposition.
        return False, f"{config.CONFIRM_TF} stack DOWN"
    m15 = _tf(coin, config.SIGNAL_TF)
    if not m15:
        return False, f"no {config.SIGNAL_TF} data"
    if stack(m15) != "UP":
        return False, f"{config.SIGNAL_TF} stack {stack(m15)}"
    return True, "ok"


def entry_candidates(snap, extras_by_sym, open_syms, reg, blocked=()):
    """Rank passing coins by 1H ADX desc. Returns (candidates, rejections):
    candidates = [(symbol, coin_dict)], rejections = [(symbol, reason)]."""
    candidates, rejections = [], []
    for coin in snap.get("symbols", []):
        sym = coin.get("symbol")
        if sym == "BTC" and reg == "risk_off":
            # BTC in confirmed downtrend is the regime, not a long candidate
            rejections.append((sym, "risk_off regime driver"))
            continue
        if sym in open_syms:
            rejections.append((sym, "already open"))
            continue
        if sym in blocked:
            rejections.append((sym, "blocked by policy"))
            continue
        ok, reason = evaluate_entry(coin, extras_by_sym.get(sym, {}), reg)
        if ok:
            candidates.append((sym, coin))
        else:
            rejections.append((sym, reason))
    candidates.sort(key=lambda c: _tf(c[1], config.QUALIFY_TF)["adx14"], reverse=True)
    return candidates, rejections


def profit_lock_trail(peak_gain, r, rungs=None):
    """ATR distance of the tightest profit-lock rung the PEAK gain has reached,
    or None while no rung is armed. Rungs are (activate_R, trail_ATR); see
    config.PROFIT_LOCK_RUNGS. Taking the minimum means a misordered rung set
    can only ever tighten."""
    rungs = config.PROFIT_LOCK_RUNGS if rungs is None else rungs
    if r <= 0:
        return None
    reached = [trail for activate, trail in rungs if peak_gain >= activate * r]
    return min(reached) if reached else None


def check_levels(position, price):
    """Test a price against the exit levels ALREADY SET on a position.

    The frozen half of check_exit: it compares, it never ratchets. Any caller
    running off-cycle uses this rather than check_exit, so that prices between
    15m closes cannot advance high_water / stop / lock — doing so would tighten
    every trail on intrabar spikes, which is a different strategy and one the
    bar-close history cannot backtest.

    Order matches check_exit exactly: stop, then lock, then take-profit. The
    time stop is absent on purpose — a clock is not a price.
    """
    if not price or price <= 0:
        return None
    stop = position.get("stop")
    if stop and price <= stop:
        return "stop"
    lock_level = position.get("lock")
    if lock_level and price <= lock_level:
        return "lock"
    entry, initial = position.get("entry_price"), position.get("initial_stop")
    if entry and initial:
        r = entry - initial
        if r > 0 and price >= entry + config.TP_R * r:
            return "tp"
    return None


def check_exit(position, signal_tf, anchor_tf, reg, hours_held):
    """Evaluate one open position.

    `signal_tf` is the 15m block and supplies PRICE — this is what buys the
    faster reaction. `anchor_tf` is the 1H block and supplies ATR — this is what
    keeps 1R the same width it was when the strategy was validated.

    Sizing R off a 15m ATR would cut stop distance to roughly a third (measured
    on BTC 2026-09-19: 15m ATR 0.217% vs 1H 0.598%), and since fee drag is a
    fraction of 1R it would push drag from 23% of 1R to 63% against a 28% cap.
    The split signature exists to make that mistake hard to make by accident.

    Returns (action, updated) where action is None|'stop'|'lock'|'tp'|'time'
    and updated is the position dict with refreshed high_water/stop/lock.

    'lock' is the profit-lock ladder (config.PROFIT_LOCK_RUNGS): once the peak
    gain has reached a rung, a level trails the high-water mark at that rung's
    ATR distance and a close at or under it exits. Armed on the PEAK, not the
    current close, so a rung once reached stays reached. It is checked after
    the stop, so an hour that falls through both is booked as the stop it is,
    and it can never fire on a new high because the level is always below it.
    """
    pos = dict(position)
    close = signal_tf["last_close"]     # 15m: reaction speed
    atr = anchor_tf.get("atr14")        # 1H: R geometry
    r = pos["entry_price"] - pos["initial_stop"]
    pos["high_water"] = max(pos.get("high_water", pos["entry_price"]), close)
    if atr and close - pos["entry_price"] >= r:
        mult = config.RISK_OFF_TRAIL_ATR_MULT if reg == "risk_off" else config.TRAIL_ATR_MULT
        pos["stop"] = max(pos["stop"], pos["high_water"] - mult * atr)
    if atr:
        trail = profit_lock_trail(pos["high_water"] - pos["entry_price"], r)
        if trail is not None:
            pos["lock"] = max(pos.get("lock") or 0.0, pos["high_water"] - trail * atr)
    # One source of truth for the comparisons, shared with the fast watcher.
    action = check_levels(pos, close)
    if action:
        return action, pos
    if hours_held >= config.TIME_STOP_HOURS:
        return "time", pos
    return None, pos
