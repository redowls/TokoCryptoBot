import json
from datetime import datetime, timezone

from tokocrypto import config, snapshot


def _bars(n, base=100.0):
    return [{"t": f"2026-09-19T00:{i:02d}:00+00:00", "o": base + i, "h": base + i + 2,
             "l": base + i - 2, "c": base + i + 1, "v": 1000.0 + i} for i in range(n)]


def test_compute_returns_none_without_bars():
    assert snapshot.compute_for_bars([]) is None


def test_compute_emits_the_indicator_block():
    out = snapshot.compute_for_bars(_bars(120))
    for key in ("last_close", "prev_close", "last_time", "ema8", "ema20", "ema55",
                "rsi14", "atr14", "adx14", "vol20", "last_vol", "bar_count"):
        assert key in out
    assert out["bar_count"] == 120


def test_compute_carries_the_previous_close_for_extras():
    bars = _bars(120)
    out = snapshot.compute_for_bars(bars)
    assert out["prev_close"] == bars[-2]["c"]


def test_single_bar_has_no_previous_close():
    assert snapshot.compute_for_bars(_bars(1))["prev_close"] is None


def test_fifteen_minute_timeframe_is_due_every_cycle():
    for minute in (0, 15, 30, 45):
        now = datetime(2026, 9, 19, 3, minute, tzinfo=timezone.utc)
        assert snapshot.due("15m", now) is True


def test_daily_timeframe_is_due_only_on_the_first_cycle_of_the_hour():
    assert snapshot.due("1D", datetime(2026, 9, 19, 3, 2, tzinfo=timezone.utc)) is True
    assert snapshot.due("1D", datetime(2026, 9, 19, 3, 17, tzinfo=timezone.utc)) is False
    assert snapshot.due("1D", datetime(2026, 9, 19, 3, 47, tzinfo=timezone.utc)) is False


def test_not_due_timeframes_carry_forward_instead_of_refetching(monkeypatch):
    calls = []

    def fake_fetch(chart, interval, **kw):
        calls.append(interval)
        return _bars(120)

    monkeypatch.setattr(snapshot.data, "fetch_klines", fake_fetch)
    carry = {"1D": {"status": "ok", "last_close": 42.0, "carried": True}}
    now = datetime(2026, 9, 19, 3, 17, tzinfo=timezone.utc)   # 1D not due
    out = snapshot.snapshot_symbol("BTC", now=now, carry=carry)
    assert "1d" not in calls                       # daily was not refetched
    assert out["timeframes"]["1D"]["last_close"] == 42.0
    assert "15m" in calls and "30m" in calls


def test_missing_carry_forces_a_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(snapshot.data, "fetch_klines",
                        lambda chart, interval, **kw: (calls.append(interval), _bars(120))[1])
    now = datetime(2026, 9, 19, 3, 17, tzinfo=timezone.utc)
    snapshot.snapshot_symbol("BTC", now=now, carry=None)
    assert "1d" in calls        # nothing to carry, so fetch anyway


def test_fetch_error_marks_the_timeframe_not_the_whole_symbol(monkeypatch):
    def boom(chart, interval, **kw):
        if interval == "4h":
            raise snapshot.data.FetchError("nope")
        return _bars(120)

    monkeypatch.setattr(snapshot.data, "fetch_klines", boom)
    out = snapshot.snapshot_symbol("BTC", now=datetime(2026, 9, 19, 3, 2, tzinfo=timezone.utc))
    assert out["status"] == "partial"
    assert out["timeframes"]["4H"]["status"] == "error"
    assert out["timeframes"]["15m"]["status"] == "ok"


def test_run_writes_hour_minute_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WATCHLIST", ["BTC"])
    monkeypatch.setattr(snapshot.data, "fetch_klines", lambda *a, **k: _bars(120))
    path = snapshot.run(now=datetime(2026, 9, 19, 14, 30, tzinfo=timezone.utc))
    assert path.name == "14-30.json"
    assert json.loads(path.read_text())["symbols"][0]["symbol"] == "BTC"


def test_run_carries_the_previous_snapshot_into_the_next_cycle(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WATCHLIST", ["BTC"])
    monkeypatch.setattr(snapshot.data, "fetch_klines", lambda *a, **k: _bars(120))
    # First cycle of the hour fetches everything.
    snapshot.run(now=datetime(2026, 9, 19, 14, 2, tzinfo=timezone.utc))
    calls = []
    monkeypatch.setattr(snapshot.data, "fetch_klines",
                        lambda chart, interval, **kw: (calls.append(interval), _bars(120))[1])
    # Second cycle should reuse 1H/4H/1D from the first.
    snapshot.run(now=datetime(2026, 9, 19, 14, 17, tzinfo=timezone.utc))
    assert set(calls) == {"15m", "30m"}


def test_the_block_records_the_closed_bar_s_high_beside_its_close():
    """The peak ladder needs a price that actually traded, not just the close."""
    bars = [{"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0,
             "t": f"2026-09-25T00:{i:02d}:00+00:00"} for i in range(60)]
    bars[-1]["h"] = 9.0
    block = snapshot.compute_for_bars(bars)
    assert block["last_high"] == 9.0
    assert block["last_close"] == 1.5
