"""Self-review report: what exists, what the backtest says, what changed.

Rendered every 4 hours by the `tokocrypto-review` Claude routine and delivered
to Telegram. It is deliberately a plain state dump plus a verdict — the routine
reads it, decides what to do, and the human reads the same numbers the routine
saw.

The report leads with the BACKTEST VERDICT, not with equity or P&L. While
TRADING_ENABLED is false the equity number is meaningless and the verdict is the
only thing that decides whether this bot should ever be switched on.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config, ledger, scorecard

BACKTEST_CACHE = config.MEMORY_DIR / "last-backtest.json"


def _sh(args, cwd=None, timeout=300):
    try:
        r = subprocess.run(args, cwd=cwd or str(config.ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _tests():
    rc, out, _ = _sh([str(config.ROOT / ".venv" / "bin" / "python"),
                      "-m", "pytest", "tests/", "-q"])
    count = None
    for line in reversed(out.splitlines()):
        if " passed" in line:
            for tok in line.replace("=", " ").split():
                if tok.isdigit():
                    count = int(tok)
                    break
            break
    return rc == 0, count


def _last_snapshot():
    try:
        days = sorted(config.DATA_DIR.glob("*"))
        if not days:
            return None
        files = sorted(days[-1].glob("*-*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text()).get("captured_at")
    except Exception:
        return None


def load_backtest():
    """The most recent cached backtest result, or None."""
    try:
        return json.loads(BACKTEST_CACHE.read_text())
    except (OSError, ValueError):
        return None


def save_backtest(result):
    """Cache a backtest result so the next review can compare against it."""
    payload = {k: v for k, v in result.items() if k != "closed"}
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    BACKTEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BACKTEST_CACHE.write_text(json.dumps(payload, indent=2))
    return payload


def collect(now=None):
    """Everything the review needs, gathered from disk and git."""
    now = now or datetime.now(timezone.utc)
    tests_ok, test_count = _tests()
    led = ledger.load()
    _, head, _ = _sh(["git", "rev-parse", "--short", "HEAD"])
    _, subject, _ = _sh(["git", "log", "-1", "--format=%s"])
    modules = len(list((config.ROOT / "tokocrypto").glob("*.py")))
    return {
        "at": now.isoformat(timespec="seconds"),
        "modules": modules,
        "tests_passing": tests_ok,
        "test_count": test_count,
        "has_credentials": bool(config.TOKO_KEY and config.TOKO_SECRET),
        "trading_enabled": bool(config.TRADING_ENABLED),
        "open_positions": len(led.get("open") or []),
        "closed_trades": len(led.get("closed") or []),
        "last_snapshot": _last_snapshot(),
        "git_head": head or "unknown",
        "git_subject": subject or "",
        "backtest": load_backtest(),
    }


def _delta(now_v, prev_v, fmt="{:+.2f}"):
    if now_v is None or prev_v is None:
        return ""
    try:
        return "  (" + fmt.format(now_v - prev_v) + " vs last)"
    except (TypeError, ValueError):
        return ""


def render(state, previous=None):
    bt = state.get("backtest")
    lines = []

    if not bt:
        lines.append("VERDICT: no backtest on record — the go-live gate has not run")
    else:
        lines.append(f"VERDICT: {bt.get('verdict', 'unknown')}")
        prev = (previous or {}).get("backtest") or {}
        net = bt.get("net_pct") or 0.0
        start = config.fmt_usdt(bt.get("start_equity", 0))
        end = config.fmt_usdt(bt.get("end_equity", 0))
        lines += [
            "",
            f"  equity     {start} -> {end}  ({net:+.2f}%)"
            f"{_delta(net, prev.get('net_pct'))}",
        ]
        if bt.get("buy_and_hold") is not None:
            lines.append(f"  hold BTC   {config.fmt_usdt(bt['buy_and_hold'])}")
        lines += [
            f"  trades     {bt.get('trades', 0)}",
            f"  true win   {bt.get('true_win_rate', 0):.0%}   "
            f"(WIN {bt.get('win', 0)} / SCRATCH {bt.get('scratch', 0)} / "
            f"FAIL {bt.get('fail', 0)})",
            f"  stop rate  {bt.get('stop_rate', 0):.0%}   "
            "<- a stop is a FAIL whatever the P&L sign",
        ]
        if bt.get("profit_factor"):
            lines.append(f"  profit f.  {bt['profit_factor']:.2f}")

    # Never let the report imply the bot is trading when it is not.
    if state["trading_enabled"]:
        status = "LIVE — real funds at risk"
    elif state["has_credentials"]:
        status = "not trading (TRADING_ENABLED is false); credentials present"
    else:
        status = "not trading (trading disabled, no credentials set)"

    lines += [
        "",
        "BUILD",
        f"  status     {status}",
        f"  tests      {'PASS' if state['tests_passing'] else 'FAIL'}"
        f" ({state['test_count'] if state['test_count'] is not None else '?'})",
        f"  modules    {state['modules']}",
        f"  positions  {state['open_positions']} open, "
        f"{state['closed_trades']} closed",
        f"  snapshot   {state['last_snapshot'] or 'none captured'}",
        f"  head       {state['git_head']} {state['git_subject']}"[:110],
    ]

    if not state["tests_passing"]:
        lines += ["", "  TESTS ARE RED. Fix before anything else."]
    elif bt and str(bt.get("verdict", "")).startswith("NO DEMONSTRATED EDGE"):
        lines += [
            "",
            "  Not a tuning prompt. Do not widen stops or loosen filters to make",
            "  this pass. Repeated failure escalates to retire-or-rebuild.",
        ]
    return "\n".join(lines)


def main(argv=None):
    state = collect()
    print(render(state))
    return 0


if __name__ == "__main__":
    main()
