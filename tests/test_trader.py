import json
from datetime import datetime, timedelta, timezone

import pytest
from tokocrypto import config, trader


def _tf(close=100.0, prev=99.0, atr=2.0):
    return {"status": "ok", "last_close": close, "prev_close": prev,
            "ema8": 103.0, "ema20": 101.0, "ema55": 99.0,
            "adx14": 30.0, "rsi14": 55.0, "atr14": atr,
            "vol20": 1000.0, "last_vol": 1200.0, "bar_count": 300}


def _coin(symbol="ETH", **over):
    tfs = {"15m": _tf(atr=0.1), "30m": _tf(), "1H": _tf(atr=2.0),
           "4H": _tf(), "1D": _tf(close=100.0, prev=99.0)}
    tfs.update(over)
    return {"symbol": symbol, "pair": config.pair(symbol), "status": "ok",
            "timeframes": tfs}


def _write_snap(tmp_path, when, symbols=None, captured_at=None):
    d = tmp_path / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{when.strftime('%H-%M')}.json"
    p.write_text(json.dumps({"captured_at": (captured_at or when).isoformat(),
                             "symbols": symbols if symbols is not None else []}))
    return p


# --- snapshot freshness --------------------------------------------------

def test_loads_a_fresh_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    _write_snap(tmp_path, datetime(2026, 9, 19, 14, 32, tzinfo=timezone.utc))
    assert trader.load_current_snapshot(now=now) is not None


def test_rejects_a_snapshot_older_than_the_cycle_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    stale = now - timedelta(minutes=config.SNAPSHOT_MAX_AGE_MIN + 5)
    _write_snap(tmp_path, stale)
    assert trader.load_current_snapshot(now=now) is None


def test_no_snapshot_directory_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert trader.load_current_snapshot(
        now=datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)) is None


def test_picks_the_newest_of_several_snapshots(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    _write_snap(tmp_path, datetime(2026, 9, 19, 14, 2, tzinfo=timezone.utc))
    _write_snap(tmp_path, datetime(2026, 9, 19, 14, 32, tzinfo=timezone.utc))
    snap = trader.load_current_snapshot(now=now)
    assert snap["captured_at"].endswith("14:32:00+00:00")


# --- the signal/anchor split ---------------------------------------------

def test_exit_check_passes_signal_and_anchor_timeframes(monkeypatch):
    """The 15m block supplies price, the 1H block supplies ATR."""
    seen = {}

    def fake_check_exit(position, signal_tf, anchor_tf, reg, hours_held):
        seen.update(signal=signal_tf, anchor=anchor_tf)
        return None, position

    monkeypatch.setattr(trader.strategy, "check_exit", fake_check_exit)
    snap = {"captured_at": "2026-09-19T14:32:00+00:00", "symbols": [_coin("ETH")]}
    pos = {"symbol": "ETH", "entry_price": 9.0, "qty": 1.0, "stop": 8.0,
           "initial_stop": 8.0, "high_water": 10.0}
    trader.check_one_exit(snap, pos, "risk_on", 1.0)
    assert seen["signal"]["atr14"] == 0.1      # 15m block
    assert seen["anchor"]["atr14"] == 2.0      # 1H block — the R anchor


def test_exit_check_is_skipped_when_a_block_is_missing():
    snap = {"symbols": [{"symbol": "ETH", "timeframes": {"15m": _tf()}}]}
    pos = {"symbol": "ETH", "entry_price": 9.0, "qty": 1.0, "stop": 8.0}
    action, out = trader.check_one_exit(snap, pos, "risk_on", 1.0)
    assert action is None
    assert out is pos


def test_marks_come_from_the_signal_timeframe():
    snap = {"symbols": [_coin("ETH", **{"15m": _tf(close=123.0, atr=0.1)})]}
    assert trader.prices_from_snapshot(snap)["ETH"] == 123.0


# --- extras --------------------------------------------------------------

def test_extras_are_derived_without_network():
    snap = {"symbols": [_coin("ETH", **{"1D": _tf(close=110.0, prev=100.0),
                                        "1H": _tf(close=50.0, prev=49.0, atr=2.0)})]}
    e = trader.extras_from_snapshot(snap)["ETH"]
    assert e["day_change_pct"] == pytest.approx(10.0)
    assert e["last_1h_close"] == 50.0
    assert e["prev_1h_close"] == 49.0


def test_extras_tolerate_a_missing_previous_close():
    snap = {"symbols": [_coin("ETH", **{"1D": _tf(close=110.0, prev=None)})]}
    assert trader.extras_from_snapshot(snap)["ETH"]["day_change_pct"] is None


# --- safety --------------------------------------------------------------

def test_dry_run_places_no_orders(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "TOKO_KEY", None)
    monkeypatch.setattr(trader.broker, "market_buy_quote",
                        lambda *a, **k: pytest.fail("dry run must not place orders"))
    monkeypatch.setattr(trader.broker, "market_sell_qty",
                        lambda *a, **k: pytest.fail("dry run must not place orders"))
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    _write_snap(tmp_path, datetime(2026, 9, 19, 14, 32, tzinfo=timezone.utc),
                symbols=[_coin("ETH")])
    trader.run(dry_run=True, now=now)


def test_trading_disabled_blocks_the_cycle(monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    monkeypatch.setattr(trader.broker, "market_buy_quote",
                        lambda *a, **k: pytest.fail("must not trade while disabled"))
    monkeypatch.setattr(trader, "_run",
                        lambda *a, **k: pytest.fail("must not reach the cycle"))
    trader.run(dry_run=False)


def test_stale_snapshot_skips_the_cycle(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    _write_snap(tmp_path, now - timedelta(minutes=90))
    trader.run(dry_run=True, now=now)
    assert "no fresh snapshot" in capsys.readouterr().out


def test_order_outcome_reads_the_mbx_fields():
    status, price, qty = trader._order_outcome(
        {"status": 2, "executedQty": "0.5", "executedPrice": "101.5"})
    assert (status, price, qty) == (2, 101.5, 0.5)


def test_order_outcome_handles_an_unfilled_order():
    status, price, qty = trader._order_outcome({"status": 0})
    assert qty == 0.0 and price is None
