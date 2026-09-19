import hashlib
import hmac
import urllib.parse

import pytest
from tokocrypto import broker, config


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(config, "TOKO_KEY", "KEY123")
    monkeypatch.setattr(config, "TOKO_SECRET", "SECRET456")


def _capture(monkeypatch, payload):
    """Patch requests.request and return the dict that records the call."""
    seen = {}

    def fake_request(method, url, headers=None, timeout=None):
        seen.update(method=method, url=url, headers=headers)
        return _FakeResp(payload)

    monkeypatch.setattr(broker.requests, "request", fake_request)
    return seen


def _query(seen):
    return urllib.parse.parse_qs(seen["url"].split("?", 1)[1])


# --- signing -------------------------------------------------------------

def test_signature_is_hmac_sha256_over_the_sent_query_string(monkeypatch):
    seen = _capture(monkeypatch, {"code": 0, "data": {}})
    broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    qs = seen["url"].split("?", 1)[1]
    sent, sig = qs.rsplit("&signature=", 1)
    expected = hmac.new(b"SECRET456", sent.encode(), hashlib.sha256).hexdigest()
    assert sig == expected
    assert seen["headers"]["X-MBX-APIKEY"] == "KEY123"


def test_request_includes_timestamp_and_recv_window(monkeypatch):
    seen = _capture(monkeypatch, {"code": 0, "data": {}})
    broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    q = _query(seen)
    assert "timestamp" in q
    assert q["recvWindow"] == [str(config.RECV_WINDOW_MS)]


def test_nonzero_code_raises_brokererror(monkeypatch):
    _capture(monkeypatch, {"code": -2015,
                           "msg": "Invalid API-key, IP, or permissions"})
    with pytest.raises(broker.BrokerError) as e:
        broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    assert "-2015" in str(e.value)


def test_missing_credentials_raises_before_any_network_call(monkeypatch):
    monkeypatch.setattr(config, "TOKO_KEY", None)
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(broker.BrokerError):
        broker.get_asset("BTC")


def test_http_failure_raises_brokererror(monkeypatch):
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: _FakeResp({}, status=418))
    with pytest.raises(broker.BrokerError):
        broker.get_asset("BTC")


# --- orders --------------------------------------------------------------

def test_market_buy_sends_quote_order_qty_and_side_zero(monkeypatch):
    seen = _capture(monkeypatch, {"code": 0, "data": {"orderId": "77"}})
    oid = broker.market_buy_quote("BTC_USDT", 50.0)
    q = _query(seen)
    assert seen["method"] == "POST"
    assert q["side"] == ["0"] and q["type"] == ["2"]
    assert q["quoteOrderQty"] == ["50.0"]
    assert "quantity" not in q
    assert oid == "77"


def test_market_sell_sends_quantity_and_side_one(monkeypatch):
    seen = _capture(monkeypatch, {"code": 0, "data": {"orderId": "78"}})
    broker.market_sell_qty("BTC_USDT", 0.5)
    q = _query(seen)
    assert q["side"] == ["1"] and q["type"] == ["2"]
    assert q["quantity"] == ["0.5"]
    assert "quoteOrderQty" not in q


def test_order_response_without_an_id_is_an_error(monkeypatch):
    _capture(monkeypatch, {"code": 0, "data": {}})
    with pytest.raises(broker.BrokerError):
        broker.market_buy_quote("BTC_USDT", 50.0)


def test_close_position_rounds_to_the_step_size(monkeypatch):
    seen = _capture(monkeypatch, {"code": 0, "data": {"orderId": "79"}})
    monkeypatch.setattr(broker.symbols, "round_qty", lambda p, q: 0.123)
    broker.close_position("BTC_USDT", 0.12345678)
    assert _query(seen)["quantity"] == ["0.123"]


# --- fills ---------------------------------------------------------------

FILLS = {"code": 0, "data": {"list": [
    {"tradeId": "1", "orderId": "77", "symbol": "BTC_USDT", "price": "100.0",
     "qty": "0.3", "quoteQty": "30.0", "commission": "0.03",
     "commissionAsset": "USDT", "taxAmount": "0.063", "taxRate": "0.0021",
     "isBuyer": 1, "isMaker": 0},
    {"tradeId": "2", "orderId": "77", "symbol": "BTC_USDT", "price": "102.0",
     "qty": "0.2", "quoteQty": "20.4", "commission": "0.02",
     "commissionAsset": "USDT", "taxAmount": "0.042", "taxRate": "0.0021",
     "isBuyer": 1, "isMaker": 0},
]}}


def test_fill_summary_sums_commission_and_tax(monkeypatch):
    _capture(monkeypatch, FILLS)
    s = broker.fill_summary("BTC_USDT", "77")
    assert s["qty"] == pytest.approx(0.5)
    assert s["price"] == pytest.approx(50.4 / 0.5)          # quote-weighted
    assert s["commission"] == pytest.approx(0.05 + 0.105)   # fee AND tax
    assert len(s["fills"]) == 2


def test_fill_summary_ignores_commission_only(monkeypatch):
    """Dropping taxAmount would understate cost by two thirds here."""
    _capture(monkeypatch, FILLS)
    s = broker.fill_summary("BTC_USDT", "77")
    assert s["commission"] > 0.05


def test_fill_summary_ignores_other_orders_fills(monkeypatch):
    _capture(monkeypatch, {"code": 0, "data": {"list": [
        {"tradeId": "9", "orderId": "OTHER", "symbol": "BTC_USDT", "price": "1.0",
         "qty": "99.0", "quoteQty": "99.0", "commission": "0", "taxAmount": "0"},
    ]}})
    s = broker.fill_summary("BTC_USDT", "77")
    assert s["qty"] == 0.0
    assert s["price"] is None


# --- polling -------------------------------------------------------------

def test_wait_for_fill_returns_once_status_is_filled(monkeypatch):
    states = iter([{"code": 0, "data": {"orderId": "77", "status": 0}},
                   {"code": 0, "data": {"orderId": "77", "status": 2,
                                        "executedQty": "0.5"}}])
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: _FakeResp(next(states)))
    out = broker.wait_for_fill("BTC_USDT", "77", timeout_s=30, sleep=lambda s: None)
    assert out["status"] == broker.FILLED


def test_wait_for_fill_gives_up_on_rejected(monkeypatch):
    _capture(monkeypatch, {"code": 0, "data": {"orderId": "77", "status": 5}})
    out = broker.wait_for_fill("BTC_USDT", "77", timeout_s=5, sleep=lambda s: None)
    assert out["status"] == broker.REJECTED


# --- balances ------------------------------------------------------------

def test_get_positions_skips_dust(monkeypatch):
    monkeypatch.setattr(broker, "get_asset",
                        lambda a, **k: {"asset": a, "free": "0.00000001", "locked": "0"})
    assert broker.get_positions({"BTC": 100_000.0}, assets=["BTC"]) == []


def test_get_positions_reports_value_and_free(monkeypatch):
    monkeypatch.setattr(broker, "get_asset",
                        lambda a, **k: {"asset": a, "free": "0.4", "locked": "0.1"})
    out = broker.get_positions({"BTC": 100.0}, assets=["BTC"])
    assert out[0]["qty"] == pytest.approx(0.5)
    assert out[0]["free"] == pytest.approx(0.4)
    assert out[0]["value"] == pytest.approx(50.0)


def test_get_account_sums_cash_and_holdings(monkeypatch):
    def fake_asset(a, **k):
        return {"asset": a, "free": "100.0" if a == "USDT" else "0.5", "locked": "0"}

    monkeypatch.setattr(broker, "get_asset", fake_asset)
    acct = broker.get_account({"BTC": 200.0})
    assert acct["cash"] == pytest.approx(100.0)
    assert acct["positions_value"] == pytest.approx(100.0)
    assert acct["equity"] == pytest.approx(200.0)


def test_get_account_survives_an_unreadable_asset(monkeypatch):
    def fake_asset(a, **k):
        if a == "USDT":
            return {"asset": a, "free": "100.0", "locked": "0"}
        raise broker.BrokerError("transient")

    monkeypatch.setattr(broker, "get_asset", fake_asset)
    acct = broker.get_account({"BTC": 200.0})
    assert acct["equity"] == pytest.approx(100.0)
