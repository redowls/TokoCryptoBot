from datetime import datetime, timezone

import pytest
from tokocrypto import config, data


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# [openTime, o, h, l, c, v, closeTime, quoteVol, trades, takerBase, takerQuote, ignore]
CLOSED = [1758240000000, "100.0", "120.0", "90.0", "110.0", "14814.0",
          1758240899999, "1.0", 5, "0.5", "0.5", "0"]
FORMING = [1758240900000, "110.0", "130.0", "105.0", "125.0", "10.0",
           1758241799999, "1.0", 5, "0.5", "0.5", "0"]


def test_normalizes_array_row_to_ohlcv_dict():
    b = data.normalize_kline(CLOSED)
    assert (b["o"], b["h"], b["l"], b["c"], b["v"]) == (100.0, 120.0, 90.0, 110.0, 14814.0)
    assert b["t"] == datetime.fromtimestamp(1758240000, timezone.utc).isoformat()


def test_unwraps_the_code_data_envelope(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": 0, "data": [CLOSED]}))
    assert len(data.fetch_klines("BTCUSDT", "15m", now_ms=1758240900000)) == 1


def test_accepts_a_bare_list_payload_too(monkeypatch):
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp([CLOSED]))
    assert len(data.fetch_klines("BTCUSDT", "15m", now_ms=1758240900000)) == 1


def test_drops_the_still_forming_final_candle(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": 0, "data": [CLOSED, FORMING]}))
    bars = data.fetch_klines("BTCUSDT", "15m", now_ms=1758241000000)
    assert len(bars) == 1
    assert bars[0]["c"] == 110.0     # the closed bar, not the forming one


def test_keeps_the_final_candle_once_it_has_closed(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": 0, "data": [CLOSED, FORMING]}))
    bars = data.fetch_klines("BTCUSDT", "15m", now_ms=1758241800000)
    assert len(bars) == 2


def test_closed_only_false_keeps_everything(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": 0, "data": [CLOSED, FORMING]}))
    bars = data.fetch_klines("BTCUSDT", "15m", closed_only=False, now_ms=1758241000000)
    assert len(bars) == 2


def test_sends_symbol_interval_and_limit(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _FakeResp({"code": 0, "data": []})

    monkeypatch.setattr(data.requests, "get", fake_get)
    data.fetch_klines("BTCUSDT", "30m", limit=300)
    assert seen["params"]["symbol"] == "BTCUSDT"
    assert seen["params"]["interval"] == "30m"
    assert seen["params"]["limit"] == 300
    assert seen["url"].endswith("/api/v3/klines")
    assert seen["headers"]["User-Agent"] == config.USER_AGENT


def test_start_and_end_are_sent_as_milliseconds(monkeypatch):
    seen = {}
    monkeypatch.setattr(data.requests, "get",
                        lambda url, params=None, **k: (seen.update(params=params),
                                                       _FakeResp({"code": 0, "data": []}))[1])
    data.fetch_klines("BTCUSDT", "15m", start=1758240000000, end=1758241800000)
    assert seen["params"]["startTime"] == 1758240000000
    assert seen["params"]["endTime"] == 1758241800000


def test_malformed_row_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": 0, "data": [CLOSED, ["junk"]]}))
    assert len(data.fetch_klines("BTCUSDT", "15m", now_ms=1758240900000)) == 1


def test_error_envelope_raises_fetcherror(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"code": -1121, "msg": "Invalid symbol"}))
    with pytest.raises(data.FetchError):
        data.fetch_klines("NOPEUSDT", "15m")


def test_http_failure_raises_fetcherror(monkeypatch):
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp({}, status=418))
    with pytest.raises(data.FetchError):
        data.fetch_klines("BTCUSDT", "15m")
