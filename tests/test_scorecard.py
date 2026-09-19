from datetime import datetime, timezone

import pytest
from tokocrypto import replay, scorecard


def _led(trades, open_=()):
    return {"open": list(open_), "closed": list(trades), "last_entry_attempt": {}}


TP = {"pnl": 25.0, "r_multiple": 2.0, "reason": "tp", "fees": 1.0}
FLAT_STOP = {"pnl": 0.01, "r_multiple": 0.002, "reason": "stop", "fees": 1.0}
LOSS_STOP = {"pnl": -10.0, "r_multiple": -1.0, "reason": "stop", "fees": 1.0}


def test_scorecard_and_replay_agree_on_what_a_win_is():
    """One definition of a win, shared, so live and backtest cannot drift."""
    trades = [FLAT_STOP, TP]
    m = scorecard.metrics(led=_led(trades), equity=1000.0)
    b = replay.bucket(trades)
    assert m["true_win_rate"] == b["true_win_rate"]
    assert m["stop_rate"] == b["stop_rate"]
    assert m["win"] == b["win"]


def test_a_break_even_stop_is_not_a_win():
    m = scorecard.metrics(led=_led([FLAT_STOP]), equity=1000.0)
    assert m["win"] == 0
    assert m["fail"] == 1
    assert m["true_win_rate"] == 0.0


def test_verdict_is_gathering_before_the_decision_point():
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    m = scorecard.metrics(led=_led([TP]), equity=1000.0, now=now)
    assert m["decision_due"] is False
    assert "GATHERING" in m["verdict"]


def test_decision_needs_both_date_and_trade_count():
    """Reaching the date alone must not trigger the decision."""
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    m = scorecard.metrics(led=_led([LOSS_STOP] * 5), equity=900.0,
                          benchmark=1000.0, now=now)
    assert m["decision_due"] is False
    assert "GATHERING" in m["verdict"]


def test_stop_requires_both_criteria():
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    # 40 losing stops, and trailing a buy-and-hold -> both tripped.
    m = scorecard.metrics(led=_led([LOSS_STOP] * 40), equity=900.0,
                          benchmark=1000.0, now=now)
    assert m["decision_due"] is True
    assert "STOP LIVE TRADING" in m["verdict"]


def test_a_low_win_rate_alone_does_not_stop_trading():
    """Survivable: a low win rate is fine if the payoff carries it."""
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    m = scorecard.metrics(led=_led([LOSS_STOP] * 40), equity=1200.0,
                          benchmark=1000.0, now=now)
    assert "CONTINUE" in m["verdict"]


def test_trailing_the_benchmark_alone_does_not_stop_trading():
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    m = scorecard.metrics(led=_led([TP] * 40), equity=900.0,
                          benchmark=1000.0, now=now)
    assert "CONTINUE" in m["verdict"]


def test_render_leads_with_the_verdict():
    m = scorecard.metrics(led=_led([TP]), equity=1000.0)
    out = scorecard.render(m)
    assert out.startswith("VERDICT:")
    assert "stop rate" in out
    assert "true win rate" in out


def test_render_survives_an_empty_ledger():
    out = scorecard.render(scorecard.metrics(led=_led([])))
    assert "VERDICT" in out
