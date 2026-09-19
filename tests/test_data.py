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


# --- paging -----------------------------------------------------------------
# fetch_klines does ONE request capped at 1000 bars. Asking for 90 days used to
# return the oldest 1000 bars of that window -- a ~10-day sample from three
# months ago, reported as if it were 90 days. Silent truncation, same class of
# bug as CryptoAutoBot's unfollowed 4H cursor.

class _PagingSession:
    """Serves 15m bars from a synthetic infinite history, 1000 per request."""

    MINUTE_MS = 60_000
    STEP = 15 * MINUTE_MS

    def __init__(self, origin_ms, total):
        self.origin, self.total, self.calls = origin_ms, total, 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        start = int(params.get("startTime", self.origin))
        end = int(params.get("endTime", self.origin + self.total * self.STEP))
        limit = int(params.get("limit", 500))
        rows = []
        t = max(start - (start - self.origin) % self.STEP, self.origin)
        while t <= end and len(rows) < limit:
            if t >= self.origin + self.total * self.STEP:
                break
            rows.append([t, "1", "2", "0.5", "1.5", "10",
                         t + self.STEP - 1, "15", 3, "5", "7", "0"])
            t += self.STEP

        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def json():
                return {"code": 0, "data": rows}

        return _R()


def test_paging_spans_the_whole_window_not_just_the_first_page():
    origin = 1_600_000_000_000
    total = 3500                       # 3.5 pages of 1000
    sess = _PagingSession(origin, total)
    end = origin + total * _PagingSession.STEP

    bars = data.fetch_klines("BTCUSDT", "15m", start=origin, end=end,
                             closed_only=False, session=sess)

    assert len(bars) == total, f"expected {total} bars, got {len(bars)}"
    assert sess.calls >= 4, "a 3500-bar window cannot come from fewer than 4 pages"


def test_paging_returns_bars_in_order_without_duplicates():
    origin = 1_600_000_000_000
    sess = _PagingSession(origin, 2500)
    end = origin + 2500 * _PagingSession.STEP

    bars = data.fetch_klines("BTCUSDT", "15m", start=origin, end=end,
                             closed_only=False, session=sess)
    stamps = [b["t"] for b in bars]
    assert stamps == sorted(stamps)
    assert len(stamps) == len(set(stamps)), "paging must not repeat the edge bar"


def test_paging_terminates_when_the_feed_runs_dry():
    """A short history must not spin forever asking for more."""
    origin = 1_600_000_000_000
    sess = _PagingSession(origin, 120)
    end = origin + 5000 * _PagingSession.STEP      # ask far beyond what exists

    bars = data.fetch_klines("BTCUSDT", "15m", start=origin, end=end,
                             closed_only=False, session=sess)
    assert len(bars) == 120
    assert sess.calls <= 3, f"ran {sess.calls} requests against a dry feed"


# --- transient failures -----------------------------------------------------
# Paging turned ~35 requests per gate run into ~350. At that volume a transient
# 504 stops being unlikely and becomes near-certain, and without a retry one
# blip discards an entire multi-minute backtest. A 90-day run died exactly this
# way on LINKUSDT 4h.

class _FlakySession:
    """Fails the first `fail_times` calls, then serves one short page."""

    def __init__(self, fail_times, exc=None, status=504):
        self.fail_times, self.calls = fail_times, 0
        self.exc, self.status = exc, status

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.exc:
                raise self.exc
            raise requests_http_error(self.status)

        class _R:
            status_code = 200
            headers = {}

            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def json():
                return {"code": 0, "data": [[1_600_000_000_000, "1", "2",
                                             "0.5", "1.5", "10",
                                             1_600_000_899_999, "15", 3,
                                             "5", "7", "0"]]}

        return _R()


def requests_http_error(status):
    import requests
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Server Error", response=resp)


def test_transient_5xx_is_retried_rather_than_losing_the_run(monkeypatch):
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)
    sess = _FlakySession(fail_times=2)
    bars = data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert len(bars) == 1
    assert sess.calls == 3, "should have retried twice then succeeded"


def test_connection_errors_are_retried_too(monkeypatch):
    import requests
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)
    sess = _FlakySession(fail_times=1, exc=__import__("requests").ConnectionError("reset"))
    bars = data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert len(bars) == 1


def test_persistent_failure_still_raises(monkeypatch):
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)
    sess = _FlakySession(fail_times=99)
    with pytest.raises(data.FetchError):
        data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert sess.calls <= data.MAX_RETRIES + 1, "must give up, not retry forever"


def test_client_errors_are_not_retried(monkeypatch):
    """A 400 means the request is wrong; repeating it just burns rate limit."""
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)
    sess = _FlakySession(fail_times=99, status=400)
    with pytest.raises(data.FetchError):
        data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert sess.calls == 1


def test_418_aborts_immediately_rather_than_digging_in(monkeypatch):
    """418 is an IP ban that escalates from 2 minutes to 3 days for repeat
    offenders. Backing off inside a run that lasts minutes cannot outlive it,
    and knocking again is what extends it."""
    monkeypatch.setattr(data.time, "sleep", lambda *_: None)
    sess = _FlakySession(fail_times=99, status=418)
    with pytest.raises(data.FetchError):
        data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert sess.calls == 1, "a ban must not be retried"


def test_429_is_retried_and_honours_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(data.time, "sleep", lambda s: slept.append(s))

    class _RateLimited(_FlakySession):
        def get(self, *a, **k):
            self.calls += 1
            if self.calls <= self.fail_times:
                import requests
                resp = requests.Response()
                resp.status_code = 429
                resp.headers["Retry-After"] = "3"
                raise requests.HTTPError("429", response=resp)
            return super().get(*a, **k)

    sess = _RateLimited(fail_times=1)
    sess.calls = 0
    bars = data.fetch_klines("BTCUSDT", "15m", closed_only=False, session=sess)
    assert len(bars) == 1
    assert 3.0 in slept, f"should have waited the advertised 3s, slept {slept}"
