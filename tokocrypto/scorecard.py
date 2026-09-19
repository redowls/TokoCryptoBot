"""Pre-registered decision scorecard for the live account.

Registered 2026-09-19, before the account was funded and before a single live
trade existed, so the criterion cannot be moved afterwards to fit the result.
That is the entire point: it is very easy to find a convincing story in a dozen
trades, and a rule written after seeing the data is not a rule.

THE CRITERION

    STOP live trading and return to backtest if BOTH hold at the decision point:
      1. true win rate < 35%   (a stop is a failed trade whatever its P&L sign,
                                per standing doctrine)
      2. the account trails simply holding BTC over the same period

    Decision point: 2026-11-19 or 40 closed trades, whichever comes LATER.

Both conditions are required. One alone is survivable — a low win rate is fine
if the payoff is large enough, and trailing a raging bull market while barely
invested proves little. Both together say the strategy is neither picking
winners nor paying for its own costs.

WHAT THIS DELIBERATELY DOES NOT DO

It does not suggest a fix and it does not tune anything. If the criterion trips,
the action is to stop — not to search for the parameter that would have passed.

Bucketing is delegated to replay.bucket so the live scorecard and the backtest
can never drift into disagreeing about what a win is.
"""
import json
import sys
from datetime import datetime, timezone

from . import config, ledger, replay

REGISTERED = "2026-09-19"
DECISION_DATE = "2026-11-19"
MIN_TRADES = 40
TRUE_WIN_FLOOR = 0.35


def load_ledger():
    try:
        return json.loads((config.TRADES_DIR / "trades.json").read_text())
    except (OSError, ValueError):
        return {"open": [], "closed": [], "last_entry_attempt": {}}


def metrics(led=None, equity=None, benchmark=None, now=None):
    """Everything the criterion needs, plus the verdict."""
    led = led if led is not None else load_ledger()
    now = now or datetime.now(timezone.utc)
    closed = led.get("closed") or []

    m = dict(replay.bucket(closed))
    m.update(replay._metrics(closed))
    m["open_positions"] = len(led.get("open") or [])
    m["equity"] = equity
    m["benchmark"] = benchmark
    m["net_pnl"] = round(sum(t.get("pnl", 0.0) for t in closed), 2)
    m["registered"] = REGISTERED
    m["decision_date"] = DECISION_DATE
    m["min_trades"] = MIN_TRADES

    reached_date = now.date().isoformat() >= DECISION_DATE
    reached_trades = m["trades"] >= MIN_TRADES
    m["decision_due"] = reached_date and reached_trades

    fails_win_rate = m["true_win_rate"] < TRUE_WIN_FLOOR
    trails_hold = (benchmark is not None and equity is not None
                   and equity < benchmark)
    m["fails_win_rate"] = fails_win_rate
    m["trails_hold"] = trails_hold

    if not m["decision_due"]:
        need = []
        if not reached_date:
            need.append(f"date {DECISION_DATE}")
        if not reached_trades:
            need.append(f"{MIN_TRADES - m['trades']} more trades")
        m["verdict"] = "GATHERING — decision needs " + " and ".join(need)
    elif fails_win_rate and trails_hold:
        m["verdict"] = "STOP LIVE TRADING — both criteria tripped"
    else:
        m["verdict"] = "CONTINUE — criterion not tripped"
    return m


def render(m):
    lines = [
        f"VERDICT: {m['verdict']}",
        "",
        f"  registered    {m['registered']} (before the account was funded)",
        f"  decision at   {m['decision_date']} or {m['min_trades']} trades, "
        "whichever is later",
        "",
        f"  trades        {m['trades']}   ({m['open_positions']} open)",
        f"  true win rate {m['true_win_rate']:.0%}   "
        f"(WIN {m['win']} / SCRATCH {m['scratch']} / FAIL {m['fail']})",
        f"  stop rate     {m['stop_rate']:.0%}   "
        "<- any stop exit is a FAIL whatever the P&L sign",
        f"  net P&L       {config.fmt_usdt(m['net_pnl'])}  "
        f"(fees {config.fmt_usdt(m['fees'])})",
    ]
    if m.get("profit_factor"):
        lines.append(f"  profit factor {m['profit_factor']:.2f}")
    if m.get("equity") is not None:
        lines.append(f"  equity        {config.fmt_usdt(m['equity'])}")
    if m.get("benchmark") is not None:
        lines.append(f"  hold BTC      {config.fmt_usdt(m['benchmark'])}")
    return "\n".join(lines)


def _main(argv=None):
    print(render(metrics()))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
