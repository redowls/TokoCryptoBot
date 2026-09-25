"""Walk-forward tuner: the only sanctioned way this bot changes its own strategy.

The naive loop — backtest, adjust, backtest, adjust, six times a day — improves
the number every time and means nothing. Consecutive runs see almost identical
data, so after a week the knobs are fitted to one window's noise and the report
climbs steadily while the strategy gets worse at the future. That is data
dredging, and it succeeds whether or not an edge exists.

So a candidate must earn its place twice:

  * on TRAIN, the older slice the search is allowed to look at, and
  * on OOS, the newest slice held back and never searched against.

A knob that helps on train and not on OOS was fitted to noise, which is the
single fact this module exists to detect. Measured on this bot: a 30-day window
returned +0.33% and a 90-day window −9.51%. A tuner without a holdout would
simply discover which window flatters it.

Cost shape (measured 2026-09-19): fetching klines 51s, build_history 231s,
replay.run **0.2s**. The expensive part is building frames, so frames are built
once and cached and each candidate is nearly free. That is what makes a real
search possible inside a 6-hourly routine.

What this module may NOT do is as important as what it does. It can only write
knobs in config.TUNABLE, which excludes RISK_PCT, MAX_POSITIONS, MAX_FEE_DRAG_R
and the fee model — sizing up and making fees look cheaper both improve a
backtest without improving anything, and a search finds them first.
"""
import argparse
import json
import pickle
import time
from datetime import datetime, timedelta, timezone

from . import config, replay, review

CACHE_DIR = config.ROOT / "data" / "history-cache"
STATE_PATH = config.MEMORY_DIR / "tuning-state.json"

TRAIN_FRAC = 0.65          # older 65% searched, newest 35% held back
MIN_OOS_TRADES = 12        # below this the OOS result is noise, not evidence
MIN_TRAIN_TRADES = 20
MARGIN_PCT = 0.40          # must beat the incumbent by this on BOTH slices
MAX_CHANGES_PER_DAY = 1
CACHE_MAX_AGE_H = 20


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


# --- frames -----------------------------------------------------------------

def _cache_path(days):
    return CACHE_DIR / f"history-{days}d.pkl"


def load_history(days=90, refresh=False, now=None):
    """Frames for the window, from cache when it is fresh enough.

    Rebuilding costs minutes; a 6-hourly routine that rebuilt every run would
    spend most of its life recomputing indicators over bars that have not
    changed.
    """
    now = now or datetime.now(timezone.utc)
    path = _cache_path(days)
    if not refresh and path.exists():
        age_h = (now.timestamp() - path.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_AGE_H:
            try:
                with path.open("rb") as fh:
                    frames = pickle.load(fh)
                log(f"history: {len(frames)} frames from cache ({age_h:.1f}h old)")
                return frames
            except Exception as e:      # truncated or stale pickle
                log(f"history: cache unreadable ({e}), rebuilding")

    t0 = time.time()
    frames = replay.build_history(replay.load_klines(days=days))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(frames, fh)
    log(f"history: built {len(frames)} frames in {time.time() - t0:.0f}s")
    return frames


def split(frames, train_frac=TRAIN_FRAC):
    """Older slice to search, newest slice to validate against.

    The holdout is the NEWEST data, not a random sample: the question is
    whether a knob keeps working on data that came after the one it was fitted
    to, which is the only question live trading ever asks.
    """
    cut = int(len(frames) * train_frac)
    return frames[:cut], frames[cut:]


# --- evaluation -------------------------------------------------------------

def evaluate(frames, values):
    """Run the real engines under `values`, then restore the active overlay."""
    try:
        config.apply_tuning(values=values)
        result = replay.run(frames)
    finally:
        config.apply_tuning()           # always back to what is on disk
    bench = replay.buy_and_hold(frames)
    return {
        "net_pct": result["net_pct"],
        "trades": result["trades"],
        "profit_factor": result.get("profit_factor"),
        "true_win_rate": result.get("true_win_rate"),
        "stop_rate": result.get("stop_rate"),
        "buy_and_hold": round(bench, 2),
    }


def _grid(name):
    """Values to try for one knob: a coarse sweep of its declared range.

    Coarse on purpose. A fine grid over a 3000-frame slice finds differences
    far below the noise floor and calls them improvements.
    """
    lo, hi = config.TUNABLE[name]
    steps = 6
    out = []
    for i in range(steps + 1):
        v = lo + (hi - lo) * i / steps
        out.append(int(round(v)) if isinstance(config._DEFAULTS[name], int)
                   else round(v, 2))
    return sorted(set(out))


def search(train, oos, base_values, knobs=None, log_fn=log):
    """Coordinate descent: one knob at a time, each judged on train then OOS.

    Returns (best_values, evidence). Only moves that improve BOTH slices by
    MARGIN_PCT survive, so a knob fitted to the train window is discarded
    rather than banked.
    """
    knobs = knobs or list(config.TUNABLE)
    current = dict(base_values)
    base_train = evaluate(train, current)
    base_oos = evaluate(oos, current)
    evidence = {"baseline": {"train": base_train, "oos": base_oos},
                "accepted": [], "rejected": []}

    if base_train["trades"] < MIN_TRAIN_TRADES:
        evidence["halt"] = (f"only {base_train['trades']} train trades "
                            f"(need {MIN_TRAIN_TRADES}) — window too small to search")
        return current, evidence

    best_train, best_oos = base_train, base_oos

    for knob in knobs:
        incumbent = current.get(knob, config._DEFAULTS[knob])
        for value in _grid(knob):
            if value == incumbent:
                continue
            trial = dict(current, **{knob: value})
            t = evaluate(train, trial)
            if t["net_pct"] <= best_train["net_pct"] + MARGIN_PCT:
                continue                                  # no train gain, skip OOS
            o = evaluate(oos, trial)
            reason = _reject_reason(o, best_oos)
            if reason:
                evidence["rejected"].append(
                    {"knob": knob, "value": value, "train_net": t["net_pct"],
                     "oos_net": o["net_pct"], "why": reason})
                continue
            log_fn(f"  accept {knob} {incumbent} -> {value}: "
                   f"train {best_train['net_pct']:+.2f} -> {t['net_pct']:+.2f}, "
                   f"oos {best_oos['net_pct']:+.2f} -> {o['net_pct']:+.2f}")
            evidence["accepted"].append(
                {"knob": knob, "from": incumbent, "to": value,
                 "train_net": t["net_pct"], "oos_net": o["net_pct"]})
            current, best_train, best_oos, incumbent = trial, t, o, value

    evidence["final"] = {"train": best_train, "oos": best_oos}
    return current, evidence


def _reject_reason(cand, incumbent):
    """Why this candidate is not evidence. None means it passes."""
    if cand["trades"] < MIN_OOS_TRADES:
        return f"only {cand['trades']} OOS trades (need {MIN_OOS_TRADES})"
    if cand["net_pct"] <= incumbent["net_pct"] + MARGIN_PCT:
        return (f"OOS gain {cand['net_pct'] - incumbent['net_pct']:+.2f}pp "
                f"below the {MARGIN_PCT}pp margin — indistinguishable from noise")
    inc_pf, cand_pf = incumbent.get("profit_factor"), cand.get("profit_factor")
    if inc_pf and cand_pf and cand_pf < inc_pf:
        return f"OOS profit factor fell {inc_pf:.2f} -> {cand_pf:.2f}"
    inc_sr, cand_sr = incumbent.get("stop_rate"), cand.get("stop_rate")
    if inc_sr is not None and cand_sr is not None and cand_sr > inc_sr + 0.05:
        return f"OOS stop rate worsened {inc_sr:.0%} -> {cand_sr:.0%}"
    return None


# --- applying ---------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"history": []}


def throttled(state, now=None):
    """At most one change a day. Six changes a day is not faster learning, it
    is six chances to fit noise before any of them has been observed live."""
    now = now or datetime.now(timezone.utc)
    for entry in reversed(state.get("history", [])):
        try:
            when = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            continue
        if entry.get("action") == "apply" and now - when < timedelta(days=1):
            return True
    return False


def apply(values, evidence, state, now=None):
    now = now or datetime.now(timezone.utc)
    config.TUNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.TUNING_PATH.write_text(json.dumps(
        {"values": values, "applied_at": now.isoformat(timespec="seconds"),
         "evidence": evidence.get("final"),
         "baseline": evidence.get("baseline")}, indent=2))
    state.setdefault("history", []).append(
        {"at": now.isoformat(timespec="seconds"), "action": "apply",
         "values": values, "oos": evidence.get("final", {}).get("oos")})
    STATE_PATH.write_text(json.dumps(state, indent=2))
    return values


def check_regression(oos_now, state):
    """Has the live overlay decayed against the OOS it was accepted on?

    An accepted change is a prediction that the gain persists on data nobody
    had seen. The next run is the first chance to check that prediction, and a
    tuner that never revisits its own decisions only ever accumulates them.
    """
    for entry in reversed(state.get("history", [])):
        if entry.get("action") == "apply" and entry.get("oos"):
            was = entry["oos"].get("net_pct")
            if was is None:
                return None
            if oos_now["net_pct"] < was - 2 * MARGIN_PCT:
                return (f"OOS decayed {was:+.2f} -> {oos_now['net_pct']:+.2f} "
                        f"since {entry['at']}")
            return None
    return None


def revert(state, reason, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        config.TUNING_PATH.unlink()
    except OSError:
        pass
    state.setdefault("history", []).append(
        {"at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
         "action": "revert", "why": reason})
    STATE_PATH.write_text(json.dumps(state, indent=2))


# --- cli --------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="TokoCryptoBot walk-forward tuner")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--apply", action="store_true",
                   help="write an accepted candidate to memory/tuning.json")
    p.add_argument("--refresh", action="store_true", help="rebuild the frame cache")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    frames = load_history(days=args.days, refresh=args.refresh)
    train, oos = split(frames)
    log(f"split: {len(train)} train frames / {len(oos)} OOS frames")

    state = load_state()
    active = config.apply_tuning()
    live_oos = evaluate(oos, active)

    regression = check_regression(live_oos, state)
    if regression and args.apply:
        revert(state, regression)
        log(f"REVERTED: {regression}")
        return {"action": "revert", "why": regression}

    best, evidence = search(train, oos, active)
    changed = {k: v for k, v in best.items() if active.get(k) != v}

    out = {"action": "none", "changed": changed, "evidence": evidence,
           "live_oos": live_oos, "regression": regression}
    if evidence.get("halt"):
        log(f"HALT: {evidence['halt']}")
    elif not changed:
        log("no candidate cleared both slices — nothing applied")
    elif throttled(state):
        log("a change was already applied within 24h — holding")
        out["action"] = "throttled"
    elif args.apply:
        apply(best, evidence, state)
        out["action"] = "apply"
        log(f"APPLIED {changed}")
    else:
        out["action"] = "proposed"
        log(f"PROPOSED (use --apply to write): {changed}")

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(summarize(out))
    return out


def summarize(out):
    ev = out["evidence"]
    base, final = ev.get("baseline", {}), ev.get("final", {})
    lines = [f"TUNER: {out['action'].upper()}"]
    if ev.get("halt"):
        lines.append(f"  halt       {ev['halt']}")
    if base:
        lines += [f"  baseline   train {base['train']['net_pct']:+.2f}% "
                  f"({base['train']['trades']} trades) | "
                  f"oos {base['oos']['net_pct']:+.2f}% "
                  f"({base['oos']['trades']} trades)"]
    if final and final != base:
        lines += [f"  tuned      train {final['train']['net_pct']:+.2f}% | "
                  f"oos {final['oos']['net_pct']:+.2f}%"]
    if out["changed"]:
        lines.append(f"  change     {out['changed']}")
    lines.append(f"  rejected   {len(ev.get('rejected', []))} candidates")
    for r in ev.get("rejected", [])[:3]:
        lines.append(f"    {r['knob']}={r['value']}: {r['why']}")
    if out.get("regression"):
        lines.append(f"  REGRESSION {out['regression']}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
