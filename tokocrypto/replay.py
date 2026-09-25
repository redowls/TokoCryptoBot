"""Kline-driven backtest over the REAL strategy and risk engines.

CryptoIndodaxBot's replay could only read accumulated hourly snapshots, so it
was useless until the bot had been running for weeks. Tokocrypto serves deep
kline history on demand, so this one works on day one — which is what lets the
go-live gate come BEFORE any money is at risk rather than after.

Two properties this file exists to protect:

  * It drives `strategy` and `risk` directly. A backtest that reimplements the
    strategy tests the reimplementation, and the two drift silently.
  * No lookahead. Each frame truncates every timeframe to bars that had closed
    by that moment. A 1H block built from a bar closing after the frame lets the
    test see the future and report an edge that cannot exist live.

Reporting follows the standing stop doctrine: buckets come off R, not the sign
of P&L, and "no demonstrated edge" is a valid verdict rather than a prompt to
start turning knobs.
"""
import argparse
from contextlib import contextmanager
import json
import time
from datetime import datetime, timedelta, timezone

from . import config, data, ledger, risk, snapshot, strategy

# How much history each timeframe needs BEYOND the replay window so EMA55 and
# ADX14 are warm at the very first frame. Without this the daily rung has ~10
# bars over a 10-day window and the regime gate never leaves "neutral".
WARMUP_DAYS = {"15m": 3, "30m": 6, "1H": 12, "4H": 40, "1D": 220}

# 15m bars to skip before the first frame, so the signal rung is warm too.
WARMUP_BARS = 60


def load_klines(symbols=None, tf_keys=None, days=10, session=None, end_ms=None):
    """Raw klines per symbol per timeframe, oldest first."""
    symbols = symbols or config.WATCHLIST
    tf_keys = tf_keys or list(config.TIMEFRAMES)
    end = end_ms or int(time.time() * 1000)
    out = {}
    for sym in symbols:
        out[sym] = {}
        for tf in tf_keys:
            lookback = days + WARMUP_DAYS.get(tf, 10)
            out[sym][tf] = data.fetch_klines(
                config.chart(sym), config.TIMEFRAMES[tf], limit=1000,
                start=end - lookback * 86_400_000, end=end, session=session)
    return out


class _Indicators:
    """compute_for_bars memoised on (symbol, timeframe, bar count).

    Frames advance one 15m bar at a time, so the 1H rung only changes every
    fourth frame and the daily rung every 96th. Recomputing all five rungs per
    frame is most of the work in a naive replay and none of the information.
    """

    def __init__(self):
        self._cache = {}

    def at(self, sym, tf, bars, upto):
        key = (sym, tf, upto)
        if key not in self._cache:
            block = snapshot.compute_for_bars(bars[:upto])
            self._cache[key] = ({"status": "no_data"} if block is None
                                else {"status": "ok", **block})
        return self._cache[key]


def _visible(bars, ts):
    """How many bars had closed at or before `ts` (bars are time-ordered)."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid]["t"] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def build_history(klines, warmup_bars=WARMUP_BARS):
    """Replay frames on the 15m clock, each carrying every timeframe's
    indicators AS OF that moment.

    Frame shape matches snapshot.run()'s output, so strategy and trader helpers
    read it without adaptation.
    """
    if not klines:
        return []
    signal_bars = next(iter(klines.values())).get(config.SIGNAL_TF) or []
    ind = _Indicators()
    frames = []
    for i in range(warmup_bars, len(signal_bars)):
        ts = signal_bars[i]["t"]
        frame = {"captured_at": ts, "symbols": []}
        for sym, by_tf in klines.items():
            tfs = {}
            for tf, bars in by_tf.items():
                upto = _visible(bars, ts)
                tfs[tf] = ind.at(sym, tf, bars, upto) if upto else {"status": "no_data"}
            frame["symbols"].append({"symbol": sym, "pair": config.pair(sym),
                                     "status": "ok", "timeframes": tfs})
        frames.append(frame)
    return frames


def bucket(trades):
    """WIN / SCRATCH / FAIL off R — never off the sign of P&L.

    Standing doctrine: any stop-triggered exit is a FAILURE whatever the P&L
    sign. A +0.0% break-even stop is not a win. Counting `pnl > 0` as the win
    rate overstated USTradeBot's by 90% and hid the fact that it had no edge.
    """
    n = len(trades)
    fail = sum(1 for t in trades
               if str(t.get("reason") or "").split("/")[0] == "stop")
    win = sum(1 for t in trades
              if str(t.get("reason") or "").split("/")[0] != "stop"
              and (t.get("r_multiple") or 0) >= 1.0)
    scratch = n - win - fail
    denom = n or 1
    return {"trades": n, "win": win, "scratch": scratch, "fail": fail,
            "stop_rate": fail / denom, "true_win_rate": win / denom}


def _metrics(trades):
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    return {
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "payoff": ((gross_win / len(wins)) / (gross_loss / len(losses)))
                  if wins and losses else None,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "fees": round(sum(t.get("fees", 0.0) for t in trades), 2),
    }


def run(history=None, start_equity=None, fee_pct=None, blocked=(), max_positions=None):
    """Replay every frame through the live engines. Returns a result dict."""
    start_equity = config.DRY_RUN_EQUITY_USDT if start_equity is None else start_equity
    history = history or []
    fee_rate = (config.TAKER_FEE_PCT + config.TAX_PCT) if fee_pct is None else fee_pct
    slots_cap = max_positions or config.MAX_POSITIONS

    led = {"open": [], "closed": [], "last_entry_attempt": {}}
    cash = start_equity
    equity_curve = []
    peak_equity = start_equity

    for frame in history:
        now = datetime.fromisoformat(frame["captured_at"])
        marks = {}
        for coin in frame["symbols"]:
            tf = (coin["timeframes"] or {}).get(config.SIGNAL_TF) or {}
            if tf.get("status") == "ok":
                marks[coin["symbol"]] = tf["last_close"]

        reg = strategy.regime(frame)

        # --- exits -------------------------------------------------------
        for pos in list(led["open"]):
            sym = pos["symbol"]
            coin = next((c for c in frame["symbols"] if c["symbol"] == sym), None)
            if not coin:
                continue
            tfs = coin["timeframes"] or {}
            signal, anchor = tfs.get(config.SIGNAL_TF), tfs.get(config.ATR_ANCHOR_TF)
            if not signal or signal.get("status") != "ok":
                continue
            if not anchor or anchor.get("status") != "ok":
                anchor = signal  # degraded, but never silently
            held_h = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 3600
            action, updated = strategy.check_exit(pos, signal, anchor, reg, held_h)
            if action:
                price = signal["last_close"]
                fee = updated["qty"] * price * fee_rate
                cash += updated["qty"] * price - fee
                ledger.close_position(led, updated, price, action, now=now, exit_fee=fee)
            else:
                ledger.update_position(led, updated)

        held_value = sum(p["qty"] * marks.get(p["symbol"], p["entry_price"])
                         for p in led["open"])
        equity = cash + held_value
        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)

        if equity <= 0:
            break
        if risk.circuit_breaker_tripped(led["closed"], equity, now=now):
            continue

        # --- entries -----------------------------------------------------
        open_syms = {p["symbol"] for p in led["open"]}
        slots = slots_cap - len(open_syms)
        if slots <= 0:
            continue
        extras = {}
        for coin in frame["symbols"]:
            tfs = coin["timeframes"] or {}
            d1 = tfs.get(config.REGIME_TF) or {}
            h1 = tfs.get(config.QUALIFY_TF) or {}
            e = {"day_change_pct": None, "last_1h_close": None, "prev_1h_close": None}
            if d1.get("status") == "ok" and d1.get("prev_close"):
                e["day_change_pct"] = (d1["last_close"] / d1["prev_close"] - 1) * 100
            if h1.get("status") == "ok":
                e["last_1h_close"] = h1.get("last_close")
                e["prev_1h_close"] = h1.get("prev_close")
            extras[coin["symbol"]] = e

        candidates, _ = strategy.entry_candidates(frame, extras, open_syms, reg,
                                                  blocked=set(blocked))
        entered = 0
        for sym, coin in candidates:
            if entered >= slots:
                break
            if ledger.throttled(led, sym, now=now):
                continue
            tfs = coin["timeframes"] or {}
            anchor = tfs.get(config.ATR_ANCHOR_TF) or {}
            signal = tfs.get(config.SIGNAL_TF) or {}
            atr = anchor.get("atr14")
            price = signal.get("last_close")
            if not atr or not price:
                continue
            qty, _stop, _risk = risk.position_size(equity, price, atr,
                                                   half=(reg == "risk_off"),
                                                   sym_pair=config.pair(sym))
            notional = qty * price
            if qty <= 0 or notional > cash:
                continue
            fee = notional * fee_rate
            cash -= notional + fee
            ledger.record_entry_attempt(led, sym, now=now)
            ledger.open_position(led, sym, qty, price, atr, order_id=None,
                                 half_size=(reg == "risk_off"), now=now,
                                 entry_fee=fee)
            entered += 1

    # Mark any still-open positions at the last frame's price.
    if history:
        last = history[-1]
        for pos in led["open"]:
            tf = next((c["timeframes"].get(config.SIGNAL_TF) for c in last["symbols"]
                       if c["symbol"] == pos["symbol"]), None) or {}
            cash += pos["qty"] * (tf.get("last_close") or pos["entry_price"])
    end_equity = cash

    trades = led["closed"]
    max_dd = 0.0
    peak = start_equity
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    result = {"start_equity": start_equity, "end_equity": round(end_equity, 2),
              "net_pct": round((end_equity / start_equity - 1) * 100, 2)
                         if start_equity else 0.0,
              "max_drawdown": round(max_dd * 100, 2),
              "frames": len(history),
              "fee_rate": fee_rate,
              "closed": trades}
    result.update(bucket(trades))
    result.update(_metrics(trades))
    return result


def buy_and_hold(history, symbol="BTC", start_equity=None):
    """Terminal equity from buying `symbol` at the first frame and holding."""
    start_equity = config.DRY_RUN_EQUITY_USDT if start_equity is None else start_equity
    if not history:
        return start_equity

    def close(frame):
        for c in frame["symbols"]:
            if c["symbol"] == symbol:
                tf = (c["timeframes"] or {}).get(config.SIGNAL_TF) or {}
                return tf.get("last_close")
        return None

    first, last = close(history[0]), close(history[-1])
    if not first or not last:
        return start_equity
    return start_equity * (last / first)


def verdict(result, benchmark=None):
    """The headline judgement. Conservative by design.

    A strategy that loses money, or that a passive hold beat, has not
    demonstrated an edge — regardless of how attractive its win rate looks.
    """
    if result["trades"] == 0:
        return "NO TRADES — nothing demonstrated"
    if result["end_equity"] <= result["start_equity"]:
        return "NO DEMONSTRATED EDGE"
    if benchmark is not None and result["end_equity"] < benchmark:
        return "NO DEMONSTRATED EDGE (underperforms buy-and-hold)"
    if result["trades"] < 20:
        return "INCONCLUSIVE — too few trades to judge"
    return "PROFITABLE IN SAMPLE — needs out-of-sample confirmation"


def summarize(result, benchmark=None):
    v = verdict(result, benchmark)
    pf = result["profit_factor"]
    payoff = result["payoff"]
    lines = [
        f"VERDICT: {v}",
        "",
        f"  window        {result['frames']} frames ({config.SIGNAL_TF} clock)",
        f"  equity        {config.fmt_usdt(result['start_equity'])} -> "
        f"{config.fmt_usdt(result['end_equity'])}  ({result['net_pct']:+.2f}%)",
    ]
    if benchmark is not None:
        delta = (result["end_equity"] / benchmark - 1) * 100 if benchmark else 0.0
        lines.append(f"  buy-and-hold  {config.fmt_usdt(benchmark)}  "
                     f"({delta:+.2f}pp vs strategy)")
    lines += [
        f"  max drawdown  {result['max_drawdown']:.2f}%",
        "",
        f"  trades        {result['trades']}",
        f"  true win rate {result['true_win_rate']:.0%}   "
        f"(WIN {result['win']} / SCRATCH {result['scratch']} / FAIL {result['fail']})",
        f"  stop rate     {result['stop_rate']:.0%}   "
        "<- any stop exit is a FAIL whatever the P&L sign",
        f"  profit factor {pf:.2f}" if pf else "  profit factor n/a",
        f"  payoff        {payoff:.2f}" if payoff else "  payoff        n/a",
        f"  fees paid     {config.fmt_usdt(result['fees'])}  "
        f"(modelled at {result['fee_rate'] * 100:.3f}% per side)",
    ]
    if v.startswith("NO DEMONSTRATED EDGE"):
        lines += [
            "",
            "  This is a valid result, not a tuning prompt. Do not widen stops,",
            "  loosen filters, or search knobs to make it pass. Repeated failure",
            "  escalates to retire-or-rebuild.",
        ]
    return "\n".join(lines)


# --- the ladder lab -------------------------------------------------------
#
# Two exit changes were ported from CryptoIndodaxBot on 2026-09-25: a percent
# profit ladder (the user's own design) and ratcheting the peak off the bar high
# instead of its close. NEITHER is enabled here on the strength of having worked
# there. That bot trades hourly IDR majors; this one trades 15m USDT pairs and
# has no demonstrated edge yet — its problem is fee drag, and a ladder that cuts
# winners shorter while paying the same round trip makes fee drag WORSE before
# it makes anything better. So they are measured here first, on this bot's data.

LADDER_VARIANTS = [
    ("off (as it runs)",        (),                                          False),
    ("peak from bar high only", (),                                          True),
    ("pct 5/2.5 .. 20/16",      ((5.0, 2.5), (10.0, 6.5), (15.0, 11.0), (20.0, 16.0)), False),
    ("pct + bar high",          ((5.0, 2.5), (10.0, 6.5), (15.0, 11.0), (20.0, 16.0)), True),
    ("pct tight 3/1.5, 6/4",    ((3.0, 1.5), (6.0, 4.0), (10.0, 7.0)),       False),
    ("pct wide 8/4, 15/11",     ((8.0, 4.0), (15.0, 11.0)),                  False),
]


@contextmanager
def _exit_geometry(pct_rungs, peak_from_high):
    """Swap the two ported knobs for one run, then put them back."""
    old = (config.PROFIT_LOCK_PCT_RUNGS, config.PEAK_FROM_BAR_HIGH)
    config.PROFIT_LOCK_PCT_RUNGS = pct_rungs
    config.PEAK_FROM_BAR_HIGH = peak_from_high
    try:
        yield
    finally:
        config.PROFIT_LOCK_PCT_RUNGS, config.PEAK_FROM_BAR_HIGH = old


def _ladder_lab(args):
    klines = load_klines(days=args.days, session=None)
    history = build_history(klines)
    if not history:
        print("no history")
        return 1
    bench = buy_and_hold(history)
    print(f"window {len(history)} frames ({config.SIGNAL_TF} clock), "
          f"{args.days} days, buy-and-hold {config.fmt_usdt(bench)}")
    print(f"\n{'variant':26} {'trades':>7} {'net %':>8} {'trueWin':>8} "
          f"{'stop%':>7} {'lock%':>7} {'PF':>6} {'payoff':>7} {'fees':>10} {'maxDD':>7}")
    print("-" * 104)
    for label, rungs, peak in LADDER_VARIANTS:
        with _exit_geometry(rungs, peak):
            r = run(history=history, fee_pct=args.fee_pct, start_equity=args.equity)
        locks = sum(1 for t in r["closed"] if t.get("reason") == "lock")
        lock_pct = (locks / r["trades"] * 100) if r["trades"] else None
        print(f"{label:26} {r['trades']:>7} {r['net_pct']:>+7.2f}% "
              f"{r['true_win_rate']:>7.0%} {r['stop_rate']:>6.0%} "
              f"{(f'{lock_pct:.0f}%' if lock_pct is not None else '-'):>7} "
              f"{(r['profit_factor'] or 0):>6.2f} {(r['payoff'] or 0):>7.2f} "
              f"{config.fmt_usdt(r['fees']):>10} {r['max_drawdown']:>6.2f}%")
    print("\nA ladder that raises net% by cutting winners short still has to pay")
    print("the same round trip per trade. Read the fees column beside the net.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="TokoCryptoBot backtest")
    p.add_argument("--days", type=int, default=10, help="replay window in days")
    p.add_argument("--fee-pct", type=float, default=None,
                   help="override per-side cost as a fraction, e.g. 0.0031")
    p.add_argument("--equity", type=float, default=None, help="starting equity")
    p.add_argument("--symbols", default=None, help="comma-separated watchlist override")
    p.add_argument("--ladder", action="store_true",
                   help="score the ported exit variants (percent ladder, bar-high "
                        "peak) on this bot's own data before any is enabled")
    p.add_argument("--json", action="store_true", help="emit the raw result dict")
    p.add_argument("--no-cache", action="store_true",
                   help="do not record this run as the review's latest gate "
                        "(use for sensitivity sweeps, which are not the gate)")
    args = p.parse_args(argv)
    if args.ladder:
        return _ladder_lab(args)

    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    klines = load_klines(symbols=syms, days=args.days)
    history = build_history(klines)
    result = run(history, start_equity=args.equity, fee_pct=args.fee_pct)
    bench = buy_and_hold(history, start_equity=args.equity or config.DRY_RUN_EQUITY_USDT)
    result["buy_and_hold"] = round(bench, 2)
    result["verdict"] = verdict(result, bench)
    # Cache it so the 6-hourly review reports this verdict instead of "the
    # go-live gate has not run", and can show the delta against the previous
    # gate. Imported here, not at module scope: review -> scorecard -> replay,
    # so a top-level import would close a cycle.
    if not args.no_cache:
        from . import review
        review.save_backtest(result)
    if args.json:
        printable = {k: v for k, v in result.items() if k != "closed"}
        print(json.dumps(printable, indent=2))
    else:
        print(summarize(result, bench))
    return result


if __name__ == "__main__":
    main()
