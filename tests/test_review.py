"""The self-review report is the only thing a headless routine sees every four
hours, so the properties worth pinning are the ones that would mislead it:
the verdict must lead, a failed gate must carry its own anti-tuning warning,
and the status line must never imply the bot is trading when it is not.
"""
import json

import pytest

from tokocrypto import review


NO_EDGE = {
    "verdict": "NO DEMONSTRATED EDGE",
    "start_equity": 1000.0,
    "end_equity": 901.44,
    "net_pct": -9.86,
    "buy_and_hold": 946.40,
    "trades": 11,
    "true_win_rate": 0.09,
    "win": 1,
    "scratch": 0,
    "fail": 10,
    "stop_rate": 0.91,
    "profit_factor": 0.08,
}


def state(**overrides):
    base = {
        "at": "2026-09-19T12:00:00+00:00",
        "modules": 17,
        "tests_passing": True,
        "test_count": 171,
        "has_credentials": False,
        "trading_enabled": False,
        "open_positions": 0,
        "closed_trades": 0,
        "last_snapshot": "2026-09-19T11:47:00+00:00",
        "git_head": "8a2e713",
        "git_subject": "feat: scorecard",
        "backtest": dict(NO_EDGE),
    }
    base.update(overrides)
    return base


def test_verdict_leads_the_report_not_equity():
    """A routine that reads only the first line must still see the verdict."""
    out = review.render(state())
    assert out.splitlines()[0] == "VERDICT: NO DEMONSTRATED EDGE"


def test_failed_gate_carries_the_anti_tuning_warning():
    out = review.render(state())
    assert "Not a tuning prompt" in out
    assert "retire-or-rebuild" in out
    # The stop doctrine must be restated beside the number, not assumed.
    assert "a stop is a FAIL whatever the P&L sign" in out


def test_report_never_claims_live_while_trading_is_disabled():
    disabled = review.render(state(trading_enabled=False, has_credentials=True))
    assert "LIVE" not in disabled
    assert "TRADING_ENABLED is false" in disabled

    live = review.render(state(trading_enabled=True, has_credentials=True))
    assert "LIVE — real funds at risk" in live


def test_missing_backtest_reads_as_gate_not_run_not_as_a_pass():
    out = review.render(state(backtest=None))
    assert "the go-live gate has not run" in out.splitlines()[0]
    assert "NO DEMONSTRATED EDGE" not in out


def test_red_tests_outrank_the_backtest_verdict():
    """Both conditions can hold at once; the report must lead the BUILD block
    with the one that invalidates every other number on the page."""
    out = review.render(state(tests_passing=False, test_count=None))
    assert "TESTS ARE RED" in out
    assert "Not a tuning prompt" not in out
    assert "tests      FAIL (?)" in out


def test_backtest_cache_round_trips_and_drops_the_trade_list(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "BACKTEST_CACHE", tmp_path / "last-backtest.json")
    saved = review.save_backtest(dict(NO_EDGE, closed=[{"symbol": "BTC_USDT"}]))

    assert "closed" not in saved          # the report needs totals, not trades
    assert "recorded_at" in saved
    assert review.load_backtest()["verdict"] == "NO DEMONSTRATED EDGE"


def test_unreadable_cache_returns_none_rather_than_raising(tmp_path, monkeypatch):
    bad = tmp_path / "last-backtest.json"
    bad.write_text("{not json")
    monkeypatch.setattr(review, "BACKTEST_CACHE", bad)
    assert review.load_backtest() is None

    monkeypatch.setattr(review, "BACKTEST_CACHE", tmp_path / "absent.json")
    assert review.load_backtest() is None


@pytest.mark.parametrize("now_v, prev_v, expected", [
    (-9.86, -12.0, "(+2.14 vs last)"),
    (-9.86, None, ""),
    (None, -12.0, ""),
    (-9.86, "unparseable", ""),
])
def test_delta_is_omitted_rather_than_guessed(now_v, prev_v, expected):
    assert expected in review._delta(now_v, prev_v)
    if not expected:
        assert review._delta(now_v, prev_v) == ""


def test_replay_main_records_its_verdict_for_the_review(tmp_path, monkeypatch):
    """The gate and the report must not disagree: a replay run is what makes
    the review stop saying 'the go-live gate has not run'."""
    from tokocrypto import replay

    monkeypatch.setattr(review, "BACKTEST_CACHE", tmp_path / "last-backtest.json")
    monkeypatch.setattr(replay, "load_klines", lambda **k: {})
    monkeypatch.setattr(replay, "build_history", lambda *a, **k: [])
    monkeypatch.setattr(replay, "run", lambda *a, **k: dict(NO_EDGE, closed=[]))
    monkeypatch.setattr(replay, "buy_and_hold", lambda *a, **k: 946.40)
    monkeypatch.setattr(replay, "verdict", lambda *a, **k: "NO DEMONSTRATED EDGE")
    monkeypatch.setattr(replay, "summarize", lambda *a, **k: "")

    replay.main([])
    assert review.load_backtest()["verdict"] == "NO DEMONSTRATED EDGE"


def test_sensitivity_sweeps_do_not_overwrite_the_gate(tmp_path, monkeypatch):
    """--fee-pct sweeps explore cost assumptions; they are not the gate and
    must not be able to install a rosier verdict as the official one."""
    from tokocrypto import replay

    monkeypatch.setattr(review, "BACKTEST_CACHE", tmp_path / "last-backtest.json")
    monkeypatch.setattr(replay, "load_klines", lambda **k: {})
    monkeypatch.setattr(replay, "build_history", lambda *a, **k: [])
    monkeypatch.setattr(replay, "run", lambda *a, **k: dict(NO_EDGE, closed=[]))
    monkeypatch.setattr(replay, "buy_and_hold", lambda *a, **k: 946.40)
    monkeypatch.setattr(replay, "verdict", lambda *a, **k: "PASSES (fantasy fees)")
    monkeypatch.setattr(replay, "summarize", lambda *a, **k: "")

    replay.main(["--no-cache"])
    assert review.load_backtest() is None
