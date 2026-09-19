import pytest
from tokocrypto import config, strategy


def tf(close=100.0, ema8=103.0, ema20=101.0, ema55=99.0,
       adx=30.0, rsi=55.0, atr=2.0):
    """A timeframe block whose EMA stack is UP by default."""
    return {"status": "ok", "last_close": close, "ema8": ema8, "ema20": ema20,
            "ema55": ema55, "adx14": adx, "rsi14": rsi, "atr14": atr,
            "vol20": 1000.0, "last_vol": 1200.0, "bar_count": 300}


def down(**kw):
    return tf(ema8=97.0, ema20=99.0, ema55=101.0, **kw)


def mixed(**kw):
    return tf(ema8=100.0, ema20=102.0, ema55=99.0, **kw)


def coin(symbol="ETH", **overrides):
    tfs = {"15m": tf(), "30m": tf(), "1H": tf(), "4H": tf(), "1D": tf()}
    tfs.update(overrides)
    return {"symbol": symbol, "status": "ok", "timeframes": tfs}


EXTRAS = {"day_change_pct": 1.0, "last_1h_close": 100.0, "prev_1h_close": 99.0}


def pos(entry=100.0, initial_stop=94.0, stop=94.0, high_water=100.0):
    return {"symbol": "ETH", "entry_price": entry, "initial_stop": initial_stop,
            "stop": stop, "high_water": high_water, "qty": 1.0}


# --- stack ---------------------------------------------------------------

def test_stack_up_when_emas_are_ordered():
    assert strategy.stack(tf()) == "UP"


def test_stack_down_when_emas_are_inverted():
    assert strategy.stack(down()) == "DOWN"


def test_stack_mixed_when_neither():
    assert strategy.stack(mixed()) == "MIXED"


# --- the cascade ---------------------------------------------------------

def test_entry_accepted_when_every_rung_agrees():
    ok, why = strategy.evaluate_entry(coin(), EXTRAS, "risk_on")
    assert ok is True, why


def test_thirty_minute_down_stack_vetoes_entry():
    ok, why = strategy.evaluate_entry(coin(**{"30m": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "30m" in why


def test_thirty_minute_mixed_stack_does_not_veto():
    """30m only confirms. A MIXED 30m is indecision, not opposition."""
    ok, why = strategy.evaluate_entry(coin(**{"30m": mixed()}), EXTRAS, "risk_on")
    assert ok is True, why


def test_fifteen_minute_stack_must_be_up_to_trigger():
    ok, why = strategy.evaluate_entry(coin(**{"15m": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "15m" in why


def test_fifteen_minute_mixed_stack_does_not_trigger():
    ok, why = strategy.evaluate_entry(coin(**{"15m": mixed()}), EXTRAS, "risk_on")
    assert ok is False
    assert "15m" in why


def test_four_hour_veto_still_applies():
    ok, why = strategy.evaluate_entry(coin(**{"4H": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "4H" in why


def test_one_hour_remains_the_qualifying_timeframe():
    ok, why = strategy.evaluate_entry(coin(**{"1H": tf(adx=5.0)}), EXTRAS, "risk_on")
    assert ok is False
    assert "adx" in why.lower()


def test_qualification_is_reported_before_timing():
    """A coin that fails both RSI and the 15m trigger reports the RSI.

    The rejection log is the main window into a 15-minute bot's behaviour, and
    'not eligible' must stay distinguishable from 'eligible, not yet triggered'.
    """
    c = coin(**{"1H": tf(rsi=20.0), "15m": down()})
    ok, why = strategy.evaluate_entry(c, EXTRAS, "risk_on")
    assert ok is False
    assert "RSI" in why
    assert "15m" not in why


# --- the ATR anchor ------------------------------------------------------

def test_min_atr_pct_is_measured_on_the_one_hour_bar():
    # A thin 15m bar must not disqualify a coin whose hourly range pays its fees.
    ok, why = strategy.evaluate_entry(coin(**{"15m": tf(atr=0.01)}), EXTRAS, "risk_on")
    assert ok is True, why


def test_thin_one_hour_atr_is_rejected_on_fee_drag():
    ok, why = strategy.evaluate_entry(coin(**{"1H": tf(atr=0.01)}), EXTRAS, "risk_on")
    assert ok is False
    assert "drag" in why.lower() or "atr" in why.lower()


# --- regime --------------------------------------------------------------

def test_regime_reads_the_daily_timeframe():
    snap = {"symbols": [{"symbol": "BTC", "status": "ok",
                         "timeframes": {"1D": tf(adx=30.0)}}]}
    assert strategy.regime(snap) == "risk_on"


def test_regime_is_risk_off_when_btc_daily_stack_is_down():
    snap = {"symbols": [{"symbol": "BTC", "status": "ok",
                         "timeframes": {"1D": down(adx=30.0)}}]}
    assert strategy.regime(snap) == "risk_off"


def test_missing_btc_does_not_default_to_risk_on():
    assert strategy.regime({"symbols": []}) != "risk_on"


def test_policy_hint_may_only_tighten():
    assert strategy.effective_regime("risk_on", "risk_off") == "risk_off"
    assert strategy.effective_regime("risk_off", "risk_on") == "risk_off"


# --- exits ---------------------------------------------------------------

def test_exit_stop_uses_price_from_the_signal_timeframe():
    action, _ = strategy.check_exit(pos(), tf(close=93.0), tf(atr=2.0), "risk_on", 1.0)
    assert action == "stop"


def test_exit_trail_distance_uses_the_one_hour_atr_not_the_15m_atr():
    """The whole design rests on this.

    A 15m ATR of 0.1 would trail at 112 - 0.4 = 111.6. The 1H ATR of 2.0 must
    win, trailing at 112 - 8.0 = 104.0.
    """
    _, updated = strategy.check_exit(pos(high_water=112.0), tf(close=112.0, atr=0.1),
                                     tf(atr=2.0), "risk_on", 3.0)
    expected = 112.0 - config.TRAIL_ATR_MULT * 2.0
    assert updated["stop"] == pytest.approx(expected, rel=1e-9)


def test_risk_off_trails_tighter():
    _, updated = strategy.check_exit(pos(high_water=112.0), tf(close=112.0),
                                     tf(atr=2.0), "risk_off", 3.0)
    expected = 112.0 - config.RISK_OFF_TRAIL_ATR_MULT * 2.0
    assert updated["stop"] == pytest.approx(expected, rel=1e-9)


def test_time_stop_fires_on_wall_clock_hours_not_bar_count():
    action, _ = strategy.check_exit(pos(high_water=101.0), tf(close=100.5),
                                    tf(atr=2.0), "risk_on",
                                    config.TIME_STOP_HOURS + 1)
    assert action == "time"


def test_take_profit_at_the_configured_r_multiple():
    action, _ = strategy.check_exit(pos(), tf(close=100.0 + config.TP_R * 6.0 + 1),
                                    tf(atr=2.0), "risk_on", 1.0)
    assert action == "tp"


def test_missing_anchor_atr_does_not_move_the_stop():
    _, updated = strategy.check_exit(pos(high_water=112.0), tf(close=112.0),
                                     {"status": "ok", "last_close": 112.0,
                                      "atr14": None}, "risk_on", 3.0)
    assert updated["stop"] == 94.0


# --- candidates ----------------------------------------------------------

def test_btc_is_never_an_entry_candidate_while_risk_off():
    snap = {"symbols": [coin(symbol="BTC", **{"1D": down()})]}
    cands, _ = strategy.entry_candidates(snap, {"BTC": EXTRAS}, set(), "risk_off")
    assert cands == []


def test_candidates_rank_by_one_hour_adx():
    snap = {"symbols": [coin(symbol="ETH", **{"1H": tf(adx=25.0)}),
                        coin(symbol="SOL", **{"1H": tf(adx=45.0)})]}
    extras = {"ETH": EXTRAS, "SOL": EXTRAS}
    cands, _ = strategy.entry_candidates(snap, extras, set(), "risk_on")
    assert [s for s, _ in cands] == ["SOL", "ETH"]
