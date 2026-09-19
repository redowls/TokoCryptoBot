import math
from datetime import datetime, timezone

import pytest
from tokocrypto import config, replay


def _series(n=400, step_min=15, drift=0.25, amp=3.0, period=20, spread=1.5):
    """An uptrend with regular pullbacks.

    A monotonic ramp is useless as a fixture: RSI pins at 100 and every entry is
    rejected as a blow-off, so nothing downstream of evaluate_entry is exercised.
    The sine term supplies the retracements that keep RSI inside the entry band.

    Bars are aligned to a common END time rather than a common start, mirroring
    load_klines: at any frame the slower rungs must already be warm, which they
    are not if every timeframe begins at the same instant.
    """
    end = 1758000000000
    out = []
    for i in range(n):
        c = 100 + drift * i + amp * math.sin(2 * math.pi * i / period)
        t = end - (n - 1 - i) * step_min * 60_000
        out.append({"t": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(),
                    "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1000.0})
    return out


STEP_MIN = {"15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}


def _klines(symbols=("BTC",), n=400):
    return {s: {tf: _series(n=n, step_min=STEP_MIN[tf]) for tf in config.TIMEFRAMES}
            for s in symbols}


# --- history construction ------------------------------------------------

def test_history_frames_carry_every_timeframe():
    hist = replay.build_history(_klines(), warmup_bars=60)
    assert hist, "history must not be empty"
    frame = hist[0]
    assert "captured_at" in frame and "symbols" in frame
    assert set(frame["symbols"][0]["timeframes"]) == set(config.TIMEFRAMES)


def test_history_frames_advance_on_the_signal_clock():
    hist = replay.build_history(_klines(n=200), warmup_bars=60)
    assert len(hist) == 200 - 60


def test_no_lookahead_higher_timeframe_uses_only_closed_bars():
    """A 1H block must never include a bar that closes after the frame."""
    kl = _klines()
    hist = replay.build_history(kl, warmup_bars=60)
    frame = hist[10]
    ts = frame["captured_at"]
    h1 = frame["symbols"][0]["timeframes"]["1H"]
    assert h1["last_time"] <= ts


def test_visible_counts_only_closed_bars():
    bars = _series(n=10, step_min=15)
    assert replay._visible(bars, bars[4]["t"]) == 5
    assert replay._visible(bars, bars[0]["t"]) == 1


def test_empty_klines_give_empty_history():
    assert replay.build_history({}) == []


# --- the doctrine --------------------------------------------------------

def test_buckets_are_computed_from_r_not_pnl_sign():
    """A +0.0% stop-out is a FAIL, not a win. Standing doctrine."""
    trades = [{"pnl": 0.01, "r_multiple": 0.002, "reason": "stop"},
              {"pnl": -5.0, "r_multiple": -1.0, "reason": "stop"},
              {"pnl": 30.0, "r_multiple": 2.5, "reason": "tp"}]
    b = replay.bucket(trades)
    assert b["win"] == 1
    assert b["fail"] == 2          # both stops fail, including the profitable one
    assert b["stop_rate"] == pytest.approx(2 / 3)
    assert b["true_win_rate"] == pytest.approx(1 / 3)


def test_a_profitable_stop_is_still_a_failure():
    b = replay.bucket([{"pnl": 12.0, "r_multiple": 0.4, "reason": "stop"}])
    assert b["win"] == 0 and b["fail"] == 1


def test_suffixed_stop_reasons_still_count_as_stops():
    b = replay.bucket([{"pnl": -1.0, "r_multiple": -1.0, "reason": "stop/no-balance"}])
    assert b["fail"] == 1


def test_a_winner_below_one_r_is_a_scratch_not_a_win():
    b = replay.bucket([{"pnl": 2.0, "r_multiple": 0.3, "reason": "tp"}])
    assert b["win"] == 0 and b["scratch"] == 1


# --- verdicts ------------------------------------------------------------

def test_losing_result_is_no_demonstrated_edge():
    r = {"trades": 30, "start_equity": 1000.0, "end_equity": 940.0}
    assert replay.verdict(r) == "NO DEMONSTRATED EDGE"


def test_underperforming_buy_and_hold_is_no_demonstrated_edge():
    r = {"trades": 30, "start_equity": 1000.0, "end_equity": 1020.0}
    assert "NO DEMONSTRATED EDGE" in replay.verdict(r, benchmark=1100.0)


def test_too_few_trades_is_inconclusive_not_a_pass():
    r = {"trades": 3, "start_equity": 1000.0, "end_equity": 1200.0}
    assert "INCONCLUSIVE" in replay.verdict(r, benchmark=1000.0)


def test_no_trades_claims_nothing():
    r = {"trades": 0, "start_equity": 1000.0, "end_equity": 1000.0}
    assert "NO TRADES" in replay.verdict(r)


def test_summary_of_a_failure_forbids_knob_tuning():
    r = replay.run([], start_equity=1000.0)
    out = replay.summarize(r)
    assert "VERDICT" in out


# --- the engine ----------------------------------------------------------

def test_run_reports_the_stop_doctrine_fields():
    result = replay.run(replay.build_history(_klines()), start_equity=1000.0)
    for key in ("win", "scratch", "fail", "stop_rate", "true_win_rate",
                "trades", "end_equity", "profit_factor", "max_drawdown", "fees"):
        assert key in result, f"missing {key}"


def test_run_uses_the_real_strategy_module(monkeypatch):
    called = {"n": 0}
    real = replay.strategy.evaluate_entry

    def counting(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(replay.strategy, "evaluate_entry", counting)
    replay.run(replay.build_history(_klines()), start_equity=1000.0)
    assert called["n"] > 0, "replay must drive the real strategy, not a copy"


def test_run_uses_the_real_risk_sizing(monkeypatch):
    called = {"n": 0}
    real = replay.risk.position_size

    def counting(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(replay.risk, "position_size", counting)
    replay.run(replay.build_history(_klines()), start_equity=1000.0)
    assert called["n"] > 0, "replay must drive the real sizing, not a copy"


def test_higher_fees_reduce_terminal_equity():
    hist = replay.build_history(_klines())
    cheap = replay.run(hist, start_equity=1000.0, fee_pct=0.0)
    dear = replay.run(hist, start_equity=1000.0, fee_pct=0.02)
    assert dear["end_equity"] < cheap["end_equity"]


def test_empty_history_is_flat_not_a_crash():
    r = replay.run([], start_equity=1000.0)
    assert r["end_equity"] == 1000.0
    assert r["trades"] == 0


def test_buy_and_hold_tracks_the_signal_close():
    hist = replay.build_history(_klines())
    bench = replay.buy_and_hold(hist, "BTC", start_equity=1000.0)
    assert bench > 1000.0      # the synthetic series rises throughout


def test_buy_and_hold_on_empty_history_returns_the_stake():
    assert replay.buy_and_hold([], "BTC", start_equity=1000.0) == 1000.0
