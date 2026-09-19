from datetime import datetime, timezone

import pytest
from tokocrypto import config, ledger, risk


# --- fee model -----------------------------------------------------------

def test_modelled_fee_includes_tax_on_top_of_commission():
    notional = 1000.0
    fee = ledger._modelled_fee("BTC_USDT", notional)
    expected = notional * (config.TAKER_FEE_PCT + config.TAX_PCT)
    assert fee == pytest.approx(expected, rel=1e-9)


def test_modelled_fee_is_strictly_larger_than_commission_alone():
    """Guards the CryptoAutoBot IMP-B01 regression: tax must not be dropped."""
    notional = 1000.0
    assert ledger._modelled_fee("BTC_USDT", notional) > notional * config.TAKER_FEE_PCT


# --- ledger --------------------------------------------------------------

def _fresh():
    return {"open": [], "closed": [], "last_entry_attempt": {}}


def test_close_position_reports_net_not_gross():
    led = _fresh()
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    ledger.close_position(led, pos, exit_price=110.0, reason="tp")
    trade = led["closed"][-1]
    assert trade["pnl_gross"] == pytest.approx(10.0)
    assert trade["pnl"] < trade["pnl_gross"], "fees and tax must come off the headline"
    assert trade["fees"] > 0


def test_realised_fee_overrides_the_model_when_the_exchange_reports_one():
    led = _fresh()
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1", entry_fee=1.0)
    ledger.close_position(led, pos, exit_price=110.0, reason="tp", exit_fee=3.0)
    trade = led["closed"][-1]
    assert trade["fees"] == pytest.approx(4.0)
    assert trade["fees_estimated"] is False


def test_r_multiple_is_recorded_for_the_stop_doctrine():
    led = _fresh()
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    # stop = 100 - 3*2 = 94, so 1R = 6.0; exiting at 112 is +2R.
    ledger.close_position(led, pos, exit_price=112.0, reason="tp")
    assert led["closed"][-1]["r_multiple"] == pytest.approx(2.0)


def test_stop_uses_the_configured_atr_multiple():
    led = _fresh()
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    assert pos["initial_stop"] == pytest.approx(100.0 - config.STOP_ATR_MULT * 2.0)
    assert pos["stop"] == pos["initial_stop"]


def test_throttle_blocks_reentry_inside_the_window():
    led = _fresh()
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    ledger.record_entry_attempt(led, "BTC", now=now)
    assert ledger.throttled(led, "BTC", now=now) is True


def test_throttle_expires_after_the_window():
    from datetime import timedelta
    led = _fresh()
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    ledger.record_entry_attempt(led, "BTC", now=now)
    later = now + timedelta(hours=config.REENTRY_THROTTLE_HOURS + 1)
    assert ledger.throttled(led, "BTC", now=later) is False


def test_reconcile_drops_a_position_with_no_balance():
    led = _fresh()
    ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0, atr=2.0, order_id="1")
    notes = ledger.reconcile(led, [])
    assert led["open"] == []
    assert any("dropped" in n for n in notes)


def test_reconcile_lets_the_exchange_win_on_quantity():
    """Fees are taken in-asset, so the held quantity drifts below the fill."""
    led = _fresh()
    ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0, atr=2.0, order_id="1")
    ledger.reconcile(led, [{"symbol": "BTC", "qty": 0.999, "current_price": 100.0}])
    assert led["open"][0]["qty"] == pytest.approx(0.999)


# --- sizing --------------------------------------------------------------

def test_position_size_risks_the_configured_fraction():
    # equity 10000, risk 1.5% = 150; stop distance = 3 * ATR 2.0 = 6.0 -> 25 units.
    # Capped by max notional equity/MAX_POSITIONS = 2500 at price 100 -> 25 units.
    qty, stop, risk_usdt = risk.position_size(10_000.0, 100.0, 2.0, sym_pair="BTC_USDT")
    assert risk_usdt == pytest.approx(150.0)
    assert stop == pytest.approx(94.0)
    assert qty == pytest.approx(25.0)


def test_position_size_caps_notional_at_one_slot():
    """A tight stop must not let one position eat the whole book."""
    qty, _, _ = risk.position_size(10_000.0, 100.0, 0.1, sym_pair="BTC_USDT")
    assert qty * 100.0 <= 10_000.0 / config.MAX_POSITIONS + 1e-6


def test_position_size_returns_zero_below_the_notional_floor():
    qty, stop, _ = risk.position_size(20.0, 100.0, 2.0, sym_pair="BTC_USDT")
    assert qty == 0.0
    assert stop is None


def test_position_size_refuses_when_the_stop_would_go_below_zero():
    qty, _, _ = risk.position_size(10_000.0, 10.0, 5.0, sym_pair="BTC_USDT")
    assert qty == 0.0


def test_sizing_reason_explains_a_refusal():
    why = risk.sizing_reason(20.0, 100.0, 2.0, sym_pair="BTC_USDT")
    assert why and why != "unsizable"


# --- circuit breaker -----------------------------------------------------

def test_circuit_breaker_trips_on_rolling_24h_loss():
    now = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    closed = [{"pnl": -500.0, "exit_time": "2026-09-19T01:00:00+00:00"}]
    assert risk.circuit_breaker_tripped(closed, 10_000.0, now=now) is True


def test_circuit_breaker_ignores_losses_older_than_the_window():
    now = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    closed = [{"pnl": -500.0, "exit_time": "2026-09-17T01:00:00+00:00"}]
    assert risk.circuit_breaker_tripped(closed, 10_000.0, now=now) is False


def test_circuit_breaker_trips_on_zero_equity():
    assert risk.circuit_breaker_tripped([], 0.0) is True
