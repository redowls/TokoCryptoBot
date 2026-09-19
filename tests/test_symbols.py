import pytest
from tokocrypto import symbols

SYMBOL = {
    "type": 1, "symbol": "BTC_USDT", "baseAsset": "BTC", "quoteAsset": "USDT",
    "basePrecision": 8, "quotePrecision": 8,
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.00001000",
         "maxQty": "9000.00000000", "stepSize": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyToMarket": True},
    ],
}


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setattr(symbols, "load", lambda *a, **k: {"BTC_USDT": SYMBOL})
    return {"BTC_USDT": SYMBOL}


def test_min_notional_reads_the_notional_filter(loaded):
    assert symbols.min_notional("BTC_USDT") == 5.0


def test_step_size_reads_the_lot_size_filter(loaded):
    assert symbols.step_size("BTC_USDT") == 0.00001


def test_round_qty_floors_to_the_step(loaded):
    assert symbols.round_qty("BTC_USDT", 0.000123456) == pytest.approx(0.00012, rel=1e-9)


def test_round_qty_never_rounds_up(loaded):
    assert symbols.round_qty("BTC_USDT", 0.000019999) == pytest.approx(0.00001, rel=1e-9)


def test_meets_minimums_rejects_below_notional(loaded):
    ok, why = symbols.meets_minimums("BTC_USDT", 0.00004, 100_000.0)   # $4
    assert ok is False
    assert "notional" in why


def test_meets_minimums_accepts_at_notional(loaded):
    ok, why = symbols.meets_minimums("BTC_USDT", 0.00006, 100_000.0)   # $6
    assert ok is True, why


def test_meets_minimums_rejects_below_min_qty(loaded):
    ok, why = symbols.meets_minimums("BTC_USDT", 0.000001, 100_000_000.0)
    assert ok is False
    assert "minQty" in why


def test_unknown_symbol_falls_back_to_the_config_floor(loaded):
    assert symbols.min_notional("NOPE_USDT") == 5.0
    assert symbols.step_size("NOPE_USDT") > 0


def test_type_three_symbols_are_excluded(monkeypatch):
    rows = [SYMBOL, {**SYMBOL, "symbol": "ALCH_USDT", "type": 3}]
    monkeypatch.setattr(symbols, "_fetch", lambda session=None: rows)
    monkeypatch.setattr(symbols, "_read_cache", lambda: None)
    monkeypatch.setattr(symbols, "_write_cache", lambda d: None)
    out = symbols.load(force=True)
    assert "BTC_USDT" in out
    assert "ALCH_USDT" not in out      # different kline host, no ticker data
