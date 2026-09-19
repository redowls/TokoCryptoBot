from tokocrypto import indicators as ind


def test_ema_basic():
    # period 3 over [1,2,3,4,5]: SMA seed=2.0, k=0.5 → 3.0 → 4.0
    assert ind.ema([1, 2, 3, 4, 5], 3) == 4.0


def test_ema_insufficient():
    assert ind.ema([1, 2], 3) is None


def test_rsi_all_gains_is_100():
    closes = list(range(1, 17))  # strictly increasing
    assert ind.rsi(closes, 14) == 100.0


def test_rsi_all_losses_is_0():
    closes = list(range(17, 1, -1))  # strictly decreasing
    assert ind.rsi(closes, 14) == 0.0


def test_rsi_flat_is_50():
    assert ind.rsi([5] * 16, 14) == 50.0


def test_rsi_insufficient():
    assert ind.rsi([1, 2, 3], 14) is None


def test_atr_constant_range():
    n = 16
    highs = [12.0] * n
    lows = [10.0] * n
    closes = [11.0] * n  # TR each bar = 2.0
    assert ind.atr(highs, lows, closes, 14) == 2.0


def test_atr_insufficient():
    assert ind.atr([1, 2], [1, 1], [1, 1], 14) is None


def test_adx_strong_uptrend_is_high():
    n = 40
    highs = [float(i + 2) for i in range(n)]
    lows = [float(i) for i in range(n)]
    closes = [float(i + 1) for i in range(n)]
    val = ind.adx(highs, lows, closes, 14)
    assert val is not None and val > 20


def test_adx_insufficient():
    assert ind.adx([1, 2], [1, 1], [1, 1], 14) is None


def test_vol_avg():
    assert ind.vol_avg(list(range(1, 21)), 20) == 10.5


def test_vol_avg_insufficient():
    assert ind.vol_avg([1, 2, 3], 20) is None
