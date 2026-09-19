from tokocrypto import config


def test_pair_uses_underscore_for_open_v1_endpoints():
    assert config.pair("BTC") == "BTC_USDT"


def test_chart_omits_underscore_for_kline_host():
    assert config.chart("BTC") == "BTCUSDT"


def test_timeframes_include_the_new_timing_layer():
    assert config.TIMEFRAMES["15m"] == "15m"
    assert config.TIMEFRAMES["30m"] == "30m"
    assert set(config.TIMEFRAMES) == {"15m", "30m", "1H", "4H", "1D"}


def test_r_is_anchored_to_the_one_hour_timeframe():
    # Load-bearing: anchoring R to a faster TF collapses the fee-drag ceiling.
    assert config.ATR_ANCHOR_TF == "1H"
    assert config.QUALIFY_TF == "1H"
    assert config.SIGNAL_TF == "15m"


def test_btc_is_in_the_watchlist_because_it_drives_the_regime_gate():
    assert "BTC" in config.WATCHLIST


def test_clock_based_knobs_are_not_rescaled_for_the_faster_cycle():
    assert config.TIME_STOP_HOURS == 120
    assert config.REENTRY_THROTTLE_HOURS == 24


def test_snapshot_staleness_matches_the_fifteen_minute_cycle():
    assert config.SNAPSHOT_MAX_AGE_MIN == 20


def test_min_atr_pct_is_derived_from_the_fee_drag_ceiling():
    # Fees may not exceed MAX_FEE_DRAG_R of 1R, and 1R is STOP_ATR_MULT * ATR.
    expected = (config.OBSERVED_ROUND_TRIP_PCT
                / config.MAX_FEE_DRAG_R / config.STOP_ATR_MULT)
    assert config.MIN_ATR_PCT == expected
