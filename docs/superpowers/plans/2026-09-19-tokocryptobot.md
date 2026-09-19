# TokoCryptoBot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tokocrypto/USDT crypto trading bot that runs every 15 minutes, using 15m and 30m candles as a timing layer beneath the inherited 1D/4H/1H decision cascade, with a self-review routine that reports to Telegram every 4 hours.

**Architecture:** Port CryptoIndodaxBot's proven pipeline, replacing only the exchange adapter (Indodax TAPI → Tokocrypto MBX) and adding a timing layer. `data.py` normalises Tokocrypto klines into the `{t,o,h,l,c,v}` bar shape the pipeline already speaks, so indicators/strategy/ledger/risk port with small, enumerated diffs. Risk `1R` stays anchored to the 1H ATR even though exits evaluate on 15m closes.

**Tech Stack:** Python 3.12, `requests`, `pytest`. No async, no WebSocket, no third-party TA library — indicators are hand-rolled and already tested upstream.

**Spec:** `docs/superpowers/specs/2026-09-19-tokocryptobot-design.md`

**Source to port from:** `/root/CryptoIndodaxBot/` (191 passing tests). Read the corresponding file there before writing each port task.

## Global Constraints

- Package name is `tokocrypto`. Repo root `/root/TokoCryptoBot`, remote `https://github.com/redowls/TokoCryptoBot.git`, branch `main`.
- `QUOTE = "USDT"`. Watchlist: `BTC ETH SOL XRP DOGE AVAX LINK DOT LTC UNI`. BTC is mandatory — its absence disables entries rather than defaulting to `risk_on`.
- **`1R` is computed from the 1H ATR, never from 15m or 30m.** Load-bearing; see spec. Any change here requires re-deriving the fee-drag ceiling first.
- Clock-based knobs are NOT rescaled for the faster cycle: `TIME_STOP_HOURS=120`, `REENTRY_THROTTLE_HOURS=24`, `POLICY_MAX_AGE_HOURS=48`, circuit-breaker 24h window.
- `TRADING_ENABLED=false` until the replay harness clears the go-live gate.
- `net.force_ipv4()` is called at import in every module that makes network calls. The VPS prefers IPv6 and exchanges whitelist the v4 address.
- Two symbol spellings: `/open/v1/*` wants `BTC_USDT`, kline/depth hosts want `BTCUSDT`. Only `config.pair()` and `config.chart()` construct them.
- Stop doctrine: trades bucket WIN/SCRATCH/FAIL off `profit_R`, never off P&L sign. Stop rate is reported beside true win rate. Never widen or remove a stop to improve a metric.
- Every task ends with a commit. Run `python -m pytest tests/ -q` before each commit; it must pass.
- `.env` is gitignored and already exists with the Telegram token. Never commit credentials.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `tokocrypto/config.py` | All constants, env loading, the two symbol spellings |
| `tokocrypto/net.py` | `force_ipv4()` — port verbatim |
| `tokocrypto/indicators.py` | EMA/RSI/ATR/ADX/vol — port verbatim |
| `tokocrypto/data.py` | Kline fetch + normalise + drop the in-progress bar |
| `tokocrypto/symbols.py` | Symbol metadata cache; LOT_SIZE / NOTIONAL filters |
| `tokocrypto/snapshot.py` | Per-cycle 5-timeframe indicator capture |
| `tokocrypto/strategy.py` | The cascade, entry evaluation, exit evaluation |
| `tokocrypto/risk.py` | Position sizing, circuit breaker |
| `tokocrypto/ledger.py` | Position records, net-of-fee P&L, throttle |
| `tokocrypto/policy.py` | Daily policy overlay loader — port verbatim |
| `tokocrypto/broker.py` | Tokocrypto MBX signed REST client |
| `tokocrypto/notify.py` | Telegram fan-out |
| `tokocrypto/lock.py` | Order lock — port verbatim |
| `tokocrypto/trader.py` | The 15-minute cycle |
| `tokocrypto/replay.py` | Kline-driven backtest over the real engines |
| `tokocrypto/scorecard.py` | Stop-doctrine metrics |
| `tokocrypto/review.py` | 4-hourly self-review report builder |

---

### Task 1: Scaffold, config, and net

**Files:**
- Create: `tokocrypto/__init__.py`, `tokocrypto/config.py`, `tokocrypto/net.py`, `requirements.txt`, `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_net.py`

**Interfaces:**
- Consumes: nothing
- Produces: `config.pair(sym) -> str` (`"BTC_USDT"`), `config.chart(sym) -> str` (`"BTCUSDT"`), `config.TIMEFRAMES: dict`, `config.SIGNAL_TF`/`CONFIRM_TF`/`QUALIFY_TF`/`VETO_TF`/`REGIME_TF`/`ATR_ANCHOR_TF: str`, `config.BAR_LIMIT: dict`, `config.REFRESH_EVERY_MIN: dict`, `net.force_ipv4() -> None`

- [ ] **Step 1: Create the venv and requirements**

```bash
cd /root/TokoCryptoBot
python3 -m venv .venv
printf 'requests>=2.31\npytest>=8.0\n' > requirements.txt
.venv/bin/pip install -q -r requirements.txt
touch tokocrypto/__init__.py tests/__init__.py
```

- [ ] **Step 2: Port net.py verbatim**

```bash
cp /root/CryptoIndodaxBot/cryptoindodax/net.py /root/TokoCryptoBot/tokocrypto/net.py
cp /root/CryptoIndodaxBot/tests/test_net.py /root/TokoCryptoBot/tests/test_net.py
sed -i 's/cryptoindodax/tokocrypto/g' /root/TokoCryptoBot/tests/test_net.py
```

This file has no Indodax-specific content — it monkeypatches `socket.getaddrinfo` to filter to `AF_INET`. Read it to confirm before moving on.

- [ ] **Step 3: Write the failing config test**

```python
# tests/test_config.py
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
```

- [ ] **Step 4: Run it and watch it fail**

Run: `cd /root/TokoCryptoBot && .venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokocrypto.config'`

- [ ] **Step 5: Write config.py**

Start from `/root/CryptoIndodaxBot/cryptoindodax/config.py` (copy it, then apply the changes below). Keep `_load_dotenv`, the path constants, the indicator periods, and every strategy knob not listed here.

```python
# --- identity -------------------------------------------------------------
QUOTE = "USDT"
WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "DOT", "LTC", "UNI"]

def pair(sym: str) -> str:
    """Spelling used by /open/v1/* endpoints."""
    return f"{sym}_{QUOTE}"

def chart(sym: str) -> str:
    """Spelling used by the kline and depth hosts."""
    return f"{sym}{QUOTE}"

# --- endpoints ------------------------------------------------------------
OPEN_BASE_URL  = "https://www.tokocrypto.com"     # signed + symbol metadata
KLINE_BASE_URL = "https://www.tokocrypto.site"    # klines, depth (type-1 symbols)
SYMBOLS_PATH   = "/open/v1/common/symbols"
KLINES_PATH    = "/api/v3/klines"
USER_AGENT     = "TokoCryptoBot/1.0 (+https://github.com/redowls/TokoCryptoBot)"
RECV_WINDOW_MS = 5000

# --- timeframe cascade ----------------------------------------------------
# 1D regime -> 4H veto -> 1H qualify -> 30m confirm -> 15m trigger/exit.
TIMEFRAMES = {"15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h", "1D": "1d"}
REGIME_TF, VETO_TF, QUALIFY_TF, CONFIRM_TF, SIGNAL_TF = "1D", "4H", "1H", "30m", "15m"
ATR_ANCHOR_TF = "1H"   # load-bearing — see docs/superpowers/specs/
BAR_LIMIT        = {"15m": 500, "30m": 500, "1H": 500, "4H": 300, "1D": 300}
REFRESH_EVERY_MIN = {"15m": 15, "30m": 15, "1H": 60, "4H": 60, "1D": 60}

# --- credentials ----------------------------------------------------------
TOKO_KEY    = os.getenv("TOKOCRYPTO_API_KEY")
TOKO_SECRET = os.getenv("TOKOCRYPTO_API_SECRET")

# --- units ----------------------------------------------------------------
MIN_ORDER_USDT      = 5.0
DUST_USDT           = 5.0
DRY_RUN_EQUITY_USDT = 1_000.0
SNAPSHOT_MAX_AGE_MIN = 20

# --- cost model (seeded assumption; replaced by measured fills) ------------
TAKER_FEE_PCT = 0.001    # 0.10% per side
TAX_PCT       = 0.0021   # Indonesian withholding, charged on top
OBSERVED_ROUND_TRIP_PCT = (TAKER_FEE_PCT * 2 + TAX_PCT) * 100   # = 0.41%
MAX_FEE_DRAG_R = 0.28
MIN_ATR_PCT = OBSERVED_ROUND_TRIP_PCT / MAX_FEE_DRAG_R / STOP_ATR_MULT
FORCE_IPV4 = os.getenv("FORCE_IPV4", "true").lower() == "true"
```

Delete `PAIRS_CACHE`/`PAIRS_URL`/`BARS_URL`/`TAPI_BASE_URL`/`INDODAX_KEY`/`INDODAX_SECRET`/`MIN_ORDER_IDR`/`DUST_IDR`/`DRY_RUN_EQUITY_IDR`/`LOOKBACK_DAYS` and the `fmt_idr`/`fmt_price` IDR formatting; add `SYMBOLS_CACHE = ROOT / "data" / "symbols.json"` and:

```python
def fmt_usdt(v):
    return f"${v:,.2f}"

def fmt_price(v):
    return f"${v:,.8f}".rstrip("0").rstrip(".") if v < 1 else f"${v:,.2f}"
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd /root/TokoCryptoBot
git add tokocrypto/ tests/ requirements.txt
git commit -m "feat: scaffold package with config and IPv4 pinning

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Indicators (verbatim port)

**Files:**
- Create: `tokocrypto/indicators.py`
- Test: `tests/test_indicators.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ema(values, period)`, `rsi(closes, period=14)`, `atr(highs, lows, closes, period=14)`, `adx(highs, lows, closes, period=14)`, `vol_avg(volumes, period=20)` — all return `Optional[float]`, `None` when there is insufficient history.

- [ ] **Step 1: Copy the module and its tests**

```bash
cp /root/CryptoIndodaxBot/cryptoindodax/indicators.py /root/TokoCryptoBot/tokocrypto/
cp /root/CryptoIndodaxBot/tests/test_indicators.py /root/TokoCryptoBot/tests/
sed -i 's/cryptoindodax/tokocrypto/g' /root/TokoCryptoBot/tests/test_indicators.py
```

This module is pure math over lists of floats with zero exchange coupling. It ports without edits.

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_indicators.py -q`
Expected: PASS with no modification to `indicators.py`. If anything fails, the copy was incomplete — re-copy rather than editing the math.

- [ ] **Step 3: Commit**

```bash
git add tokocrypto/indicators.py tests/test_indicators.py
git commit -m "feat: port indicators unchanged from CryptoIndodaxBot

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Kline fetching

**Files:**
- Create: `tokocrypto/data.py`
- Test: `tests/test_data.py`

**Interfaces:**
- Consumes: `config.KLINE_BASE_URL`, `config.KLINES_PATH`, `config.USER_AGENT`, `net.force_ipv4`
- Produces: `data.FetchError`, `data.normalize_kline(row) -> dict`, `data.fetch_klines(chart_symbol, interval, limit=500, start=None, end=None, closed_only=True, now_ms=None, session=None) -> list[dict]`

**The detail that matters:** Tokocrypto returns the **currently-forming** candle as the last row. Computing EMAs and ADX on a partial bar makes every indicator flicker within the bar, and on a 15-minute cycle that flicker *is* the signal. `closed_only=True` drops any row whose close time is still in the future. This is the single most likely source of phantom signals in this bot.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_data.py
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_data.py -q`
Expected: FAIL — `No module named 'tokocrypto.data'`

- [ ] **Step 3: Write data.py**

```python
"""Tokocrypto public market data.

Klines come back as arrays, not objects, wrapped in a {"code","data"} envelope:

    [openTime, open, high, low, close, volume, closeTime, ...]

with millisecond timestamps and numeric fields as strings. `fetch_klines`
normalises them to the {"t","o","h","l","c","v"} shape the rest of the pipeline
speaks, so indicators/snapshot/strategy port from CryptoIndodaxBot untouched.

The last row is the *currently forming* candle. On a 15-minute cycle, letting a
partial bar into the indicators means every EMA and ADX flickers inside the bar
and the bot trades its own noise. `closed_only` drops it by default.
"""
import time
from datetime import datetime, timezone

import requests

from . import config, net


class FetchError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()


def normalize_kline(row):
    """One Tokocrypto kline array -> an Alpaca-shaped bar dict."""
    return {
        "t": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat(),
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]),
    }


def _rows(payload, label):
    """Unwrap {"code":0,"data":[...]} or accept a bare list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("code") not in (0, None):
            raise FetchError(f"{label}: {payload.get('msg') or payload.get('message')} "
                             f"(code {payload.get('code')})")
        rows = payload.get("data")
        if isinstance(rows, list):
            return rows
    raise FetchError(f"{label}: unexpected payload {payload!r:.120}")


def fetch_klines(chart_symbol, interval, limit=500, start=None, end=None,
                 closed_only=True, now_ms=None, session=None):
    """OHLCV bars for one chart symbol ("BTCUSDT") and interval ("15m").

    Returns a list, oldest first, possibly empty. `start`/`end` are unix
    milliseconds. With `closed_only`, any trailing bar whose close time has not
    yet passed is dropped.
    """
    label = f"{chart_symbol} {interval}"
    params = {"symbol": chart_symbol, "interval": interval, "limit": limit}
    if start is not None:
        params["startTime"] = int(start)
    if end is not None:
        params["endTime"] = int(end)
    getter = (session or requests).get
    try:
        r = getter(f"{config.KLINE_BASE_URL}{config.KLINES_PATH}", params=params,
                   headers={"User-Agent": config.USER_AGENT}, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"{label}: {e}") from e

    rows = _rows(payload, label)
    if closed_only and rows:
        cutoff = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = [r for r in rows if _close_time(r) is not None and _close_time(r) <= cutoff]

    out = []
    for row in rows:
        try:
            out.append(normalize_kline(row))
        except (IndexError, KeyError, TypeError, ValueError):
            continue  # skip a malformed bar rather than lose the whole series
    return out


def _close_time(row):
    try:
        return int(row[6])
    except (IndexError, TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_data.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Verify against the live endpoint**

```bash
cd /root/TokoCryptoBot && .venv/bin/python -c "
from tokocrypto import data, config
for tf in ('15m','30m','1h'):
    b = data.fetch_klines(config.chart('BTC'), tf, limit=5)
    print(tf, len(b), b[-1]['t'], b[-1]['c'])
"
```
Expected: 4 bars per interval (the 5th is dropped as still forming), timestamps ascending, prices near spot. If you get 5, `closed_only` is not working.

- [ ] **Step 6: Commit**

```bash
git add tokocrypto/data.py tests/test_data.py
git commit -m "feat: Tokocrypto kline fetching, dropping the in-progress bar

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Symbol metadata and order filters

**Files:**
- Create: `tokocrypto/symbols.py`
- Test: `tests/test_symbols.py`

**Interfaces:**
- Consumes: `config.OPEN_BASE_URL`, `config.SYMBOLS_PATH`, `config.SYMBOLS_CACHE`, `config.MIN_ORDER_USDT`
- Produces: `symbols.load(force=False, now=None) -> dict` keyed by `"BTC_USDT"`, `symbols.meta(sym_pair) -> dict`, `symbols.min_notional(sym_pair) -> float`, `symbols.step_size(sym_pair) -> float`, `symbols.round_qty(sym_pair, qty) -> float`, `symbols.meets_minimums(sym_pair, qty, price) -> bool`

Model on `/root/CryptoIndodaxBot/cryptoindodax/pairs.py` (24h disk cache, same structure). The difference is the filter format: Tokocrypto nests `LOT_SIZE` and `NOTIONAL` inside a `filters` array.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_symbols.py
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
    # 0.000123456 -> 0.00012, never rounded up past what we can afford
    assert symbols.round_qty("BTC_USDT", 0.000123456) == pytest.approx(0.00012, rel=1e-9)


def test_round_qty_never_rounds_up(loaded):
    assert symbols.round_qty("BTC_USDT", 0.000019999) == pytest.approx(0.00001, rel=1e-9)


def test_meets_minimums_rejects_below_notional(loaded):
    assert symbols.meets_minimums("BTC_USDT", 0.00004, 100_000.0) is False   # $4


def test_meets_minimums_accepts_at_notional(loaded):
    assert symbols.meets_minimums("BTC_USDT", 0.00006, 100_000.0) is True    # $6


def test_meets_minimums_rejects_below_min_qty(loaded):
    assert symbols.meets_minimums("BTC_USDT", 0.000001, 100_000_000.0) is False


def test_unknown_symbol_falls_back_to_the_config_floor(loaded):
    assert symbols.min_notional("NOPE_USDT") == 5.0
    assert symbols.step_size("NOPE_USDT") > 0


def test_type_three_symbols_are_excluded(monkeypatch):
    payload = {"code": 0, "data": {"list": [SYMBOL, {**SYMBOL, "symbol": "ALCH_USDT",
                                                     "type": 3}]}}
    monkeypatch.setattr(symbols, "_fetch", lambda session=None: payload["data"]["list"])
    monkeypatch.setattr(symbols, "_read_cache", lambda: None)
    monkeypatch.setattr(symbols, "_write_cache", lambda d: None)
    out = symbols.load(force=True)
    assert "BTC_USDT" in out
    assert "ALCH_USDT" not in out      # different kline host, no ticker data
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_symbols.py -q`
Expected: FAIL — `No module named 'tokocrypto.symbols'`

- [ ] **Step 3: Implement symbols.py**

```python
"""Symbol metadata: order minimums and quantity precision.

Tokocrypto nests constraints in a `filters` array, Binance-style. The two that
bind us:

  * LOT_SIZE.stepSize — quantity must be a multiple of it. Always round DOWN;
    rounding up produces an order we cannot fund.
  * NOTIONAL.minNotional — price * quantity floor, $5 on most USDT pairs.

Type-3 ("Nextme") symbols are dropped: they are served by a different kline
host and carry no ticker data, so the pipeline cannot price them.
"""
import json
import math
import time
from datetime import datetime, timezone

import requests

from . import config, net

CACHE_MAX_AGE_S = 24 * 3600
_MEM = {"at": 0.0, "data": None}

if config.FORCE_IPV4:
    net.force_ipv4()


def _fetch(session=None):
    getter = (session or requests).get
    r = getter(f"{config.OPEN_BASE_URL}{config.SYMBOLS_PATH}",
               headers={"User-Agent": config.USER_AGENT}, timeout=20)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("list") or []


def _read_cache():
    try:
        raw = json.loads(config.SYMBOLS_CACHE.read_text())
        if time.time() - raw["at"] < CACHE_MAX_AGE_S:
            return raw["data"]
    except Exception:
        return None
    return None


def _write_cache(data):
    try:
        config.SYMBOLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        config.SYMBOLS_CACHE.write_text(json.dumps({"at": time.time(), "data": data}))
    except Exception:
        pass


def load(force=False, now=None):
    """Symbol metadata keyed by pair ("BTC_USDT"). Cached on disk for 24h."""
    if not force and _MEM["data"] is not None:
        return _MEM["data"]
    data = None if force else _read_cache()
    if data is None:
        data = {s["symbol"]: s for s in _fetch() if s.get("type") == 1}
        _write_cache(data)
    _MEM["data"] = data
    return data


def meta(sym_pair):
    return load().get(sym_pair) or {}


def _filter(sym_pair, kind):
    for f in meta(sym_pair).get("filters") or []:
        if f.get("filterType") == kind:
            return f
    return {}


def min_notional(sym_pair):
    try:
        return float(_filter(sym_pair, "NOTIONAL")["minNotional"])
    except (KeyError, TypeError, ValueError):
        return config.MIN_ORDER_USDT


def step_size(sym_pair):
    try:
        return float(_filter(sym_pair, "LOT_SIZE")["stepSize"])
    except (KeyError, TypeError, ValueError):
        return 1e-8


def min_qty(sym_pair):
    try:
        return float(_filter(sym_pair, "LOT_SIZE")["minQty"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def round_qty(sym_pair, qty):
    """Floor to stepSize. Never rounds up — that funds an order we cannot pay for."""
    step = step_size(sym_pair)
    if step <= 0:
        return float(qty)
    return math.floor(float(qty) / step) * step


def meets_minimums(sym_pair, qty, price):
    if qty <= 0 or price <= 0:
        return False
    if qty < min_qty(sym_pair):
        return False
    return qty * price >= max(min_notional(sym_pair), config.MIN_ORDER_USDT)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_symbols.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Verify against the live endpoint**

```bash
.venv/bin/python -c "
from tokocrypto import symbols, config
d = symbols.load(force=True)
print('type-1 symbols cached:', len(d))
for c in config.WATCHLIST:
    p = config.pair(c)
    print(f'{p:12s} minNotional {symbols.min_notional(p):>6.2f}  step {symbols.step_size(p)}')
"
```
Expected: all ten pairs present, `minNotional` 5.00 (DOGE 1.00).

- [ ] **Step 6: Add the shared test fixture**

```python
# tests/conftest.py
import pytest

from tokocrypto import symbols

PERMISSIVE = {
    "type": 1, "symbol": "ANY_USDT", "baseAsset": "ANY", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.00000001",
         "maxQty": "9000000.0", "stepSize": "0.00000001"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


class _AnySymbol(dict):
    """Metadata for any pair, so the suite does not break when the watchlist
    changes. Real per-symbol constraints are exercised in test_symbols.py."""

    def get(self, key, default=None):
        return PERMISSIVE


@pytest.fixture(autouse=True)
def _no_network_symbol_metadata(monkeypatch, request):
    """Keep symbol metadata off the network for every test. round_qty and
    meets_minimums sit on the sizing path, so without this the suite would hit
    tokocrypto.com. test_symbols.py drives the cache itself and opts out."""
    if request.node.fspath.basename == "test_symbols.py":
        return
    monkeypatch.setattr(symbols, "load", lambda *a, **k: _AnySymbol())
```

- [ ] **Step 7: Run the whole suite and commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add tokocrypto/symbols.py tests/test_symbols.py tests/conftest.py
git commit -m "feat: symbol metadata with LOT_SIZE and NOTIONAL filters

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Snapshot across five timeframes

**Files:**
- Create: `tokocrypto/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `data.fetch_klines`, `indicators.*`, `config.TIMEFRAMES`, `config.BAR_LIMIT`, `config.REFRESH_EVERY_MIN`
- Produces: `snapshot.compute_for_bars(bars) -> dict|None`, `snapshot.due(tf_key, now) -> bool`, `snapshot.snapshot_symbol(sym, now=None, carry=None) -> dict`, `snapshot.run(now=None) -> Path`

Snapshot payload shape is unchanged from CryptoIndodaxBot — `{"captured_at", "symbols": [{"symbol","pair","status","timeframes":{tf: {...}}}]}` — so `digest.py` and `replay.py` port cleanly. Two changes: five timeframes instead of three, and the file is `HH-MM.json`.

**Rate-limit discipline:** refetching 4H and 1D every 15 minutes is 3× the necessary traffic against an exchange that IP-bans. `due()` gates each timeframe by `REFRESH_EVERY_MIN`, and `carry` supplies the previous snapshot's values for timeframes that are not due.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_snapshot.py
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
    for key in ("last_close", "last_time", "ema8", "ema20", "ema55",
                "rsi14", "atr14", "adx14", "vol20", "last_vol", "bar_count"):
        assert key in out
    assert out["bar_count"] == 120


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
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_snapshot.py -q`
Expected: FAIL — `No module named 'tokocrypto.snapshot'`

- [ ] **Step 3: Implement snapshot.py**

```python
"""Per-cycle indicator capture across the five-timeframe cascade.

The payload shape matches CryptoIndodaxBot's so digest and replay port
unchanged. Two differences: five timeframes rather than three, and the file is
named HH-MM.json because cycles are every 15 minutes.

Tokocrypto bans by IP. Refetching 4H and 1D every cycle would be three times the
necessary traffic for data that cannot have changed, so `due()` gates each
timeframe by config.REFRESH_EVERY_MIN and anything not due is carried forward
from the previous snapshot.
"""
import json
from datetime import datetime, timezone

from . import config, data, indicators


def compute_for_bars(bars):
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b["v"] for b in bars]
    return {
        "last_close": closes[-1],
        "last_time": bars[-1]["t"],
        "ema8": indicators.ema(closes, 8),
        "ema20": indicators.ema(closes, 20),
        "ema55": indicators.ema(closes, 55),
        "rsi14": indicators.rsi(closes, config.RSI_PERIOD),
        "atr14": indicators.atr(highs, lows, closes, config.ATR_PERIOD),
        "adx14": indicators.adx(highs, lows, closes, config.ADX_PERIOD),
        "vol20": indicators.vol_avg(vols, config.VOL_PERIOD),
        "last_vol": vols[-1],
        "bar_count": len(bars),
    }


def due(tf_key, now):
    """Is this timeframe scheduled for a refetch on the cycle at `now`?"""
    every = config.REFRESH_EVERY_MIN.get(tf_key, 15)
    if every <= 15:
        return True
    # Hourly timeframes refresh on the first cycle of each hour only.
    return now.minute < 15


def snapshot_symbol(sym, now=None, carry=None):
    now = now or datetime.now(timezone.utc)
    out = {"symbol": sym, "pair": config.pair(sym), "status": "ok", "timeframes": {}}
    for tf_key, interval in config.TIMEFRAMES.items():
        prior = (carry or {}).get(tf_key)
        if not due(tf_key, now) and prior and prior.get("status") == "ok":
            out["timeframes"][tf_key] = prior
            continue
        try:
            bars = data.fetch_klines(config.chart(sym), interval,
                                     limit=config.BAR_LIMIT.get(tf_key, 300))
            ind = compute_for_bars(bars)
            out["timeframes"][tf_key] = ({"status": "no_data"} if ind is None
                                         else {"status": "ok", **ind})
        except Exception as e:
            out["status"] = "partial"
            out["timeframes"][tf_key] = {"status": "error", "error": str(e)}
    return out


def _previous(now):
    """Timeframe blocks from the most recent snapshot, keyed by symbol."""
    try:
        day = config.DATA_DIR / now.strftime("%Y-%m-%d")
        files = sorted(day.glob("*.json"))
        if not files:
            return {}
        snap = json.loads(files[-1].read_text())
        return {s["symbol"]: s.get("timeframes", {}) for s in snap.get("symbols", [])}
    except Exception:
        return {}


def run(now=None):
    now = now or datetime.now(timezone.utc)
    carry_by_sym = _previous(now)
    date_dir = config.DATA_DIR / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    snap = {"captured_at": now.isoformat(), "symbols": []}
    for sym in config.WATCHLIST:
        try:
            snap["symbols"].append(
                snapshot_symbol(sym, now=now, carry=carry_by_sym.get(sym)))
        except Exception as e:
            snap["symbols"].append({"symbol": sym, "status": "error", "error": str(e)})
    path = date_dir / f"{now.strftime('%H-%M')}.json"
    path.write_text(json.dumps(snap, indent=2))
    return path


if __name__ == "__main__":
    print(f"wrote {run()}")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_snapshot.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Capture one real snapshot and inspect it**

```bash
.venv/bin/python -m tokocrypto.snapshot
.venv/bin/python -c "
import json,glob
p=sorted(glob.glob('data/snapshots/*/*.json'))[-1]
s=json.load(open(p))
b=[x for x in s['symbols'] if x['symbol']=='BTC'][0]
print(p, b['status'])
for tf,v in b['timeframes'].items():
    print(f\"  {tf:4s} {v['status']:5s} close={v.get('last_close')} adx={v.get('adx14')} atr={v.get('atr14')}\")
"
```
Expected: all five timeframes `ok`, `atr14` on 1H materially larger than on 15m. That gap is exactly why R is anchored to 1H.

- [ ] **Step 6: Commit**

```bash
git add tokocrypto/snapshot.py tests/test_snapshot.py
git commit -m "feat: five-timeframe snapshot with refresh-gated higher timeframes

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Strategy — the cascade with the timing layer

**Files:**
- Create: `tokocrypto/strategy.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Consumes: `config.*_TF`, `config.ENTRY_*`, `config.MIN_ATR_PCT`, `config.STOP_ATR_MULT`, `config.TRAIL_ATR_MULT`, `config.TP_R`
- Produces: `strategy.stack(tf) -> str` (`"UP"`/`"DOWN"`/`"MIXED"`), `strategy.regime(snap) -> str`, `strategy.effective_regime(computed, policy_hint) -> str`, `strategy.evaluate_entry(coin, extras, reg) -> (bool, str)`, `strategy.entry_candidates(snap, extras_by_sym, open_syms, reg, blocked=()) -> (list, list)`, `strategy.profit_lock_trail(peak_gain, r, rungs=None)`, `strategy.check_levels(position, price)`, `strategy.check_exit(position, signal_tf, anchor_tf, reg, hours_held) -> (str|None, dict)`

Start from `/root/CryptoIndodaxBot/cryptoindodax/strategy.py`. Read it in full first — most of it is unchanged.

**The two changes:**

1. `evaluate_entry` gains two rungs after the existing 1H and 4H checks: reject when the 30m stack is DOWN, reject when the 15m stack is not UP.
2. `check_exit` takes the 15m block for *price* and the 1H block for *ATR*. The old signature `check_exit(position, h1, reg, hours_held)` read both from the same dict; splitting them is what keeps R anchored to 1H while reacting on 15m.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_strategy.py
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


def coin(**overrides):
    tfs = {"15m": tf(), "30m": tf(), "1H": tf(), "4H": tf(), "1D": tf()}
    tfs.update(overrides)
    return {"symbol": "ETH", "status": "ok", "timeframes": tfs}


EXTRAS = {"day_pct": 1.0, "prev_closes": [99.0, 100.0]}


def test_stack_up_when_emas_are_ordered():
    assert strategy.stack(tf()) == "UP"


def test_stack_down_when_emas_are_inverted():
    assert strategy.stack(down()) == "DOWN"


def test_entry_accepted_when_every_rung_agrees():
    ok, why = strategy.evaluate_entry(coin(), EXTRAS, "risk_on")
    assert ok is True, why


def test_thirty_minute_down_stack_vetoes_entry():
    ok, why = strategy.evaluate_entry(coin(**{"30m": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "30m" in why


def test_fifteen_minute_stack_must_be_up_to_trigger():
    ok, why = strategy.evaluate_entry(coin(**{"15m": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "15m" in why


def test_fifteen_minute_mixed_stack_does_not_trigger():
    mixed = tf(ema8=100.0, ema20=102.0, ema55=99.0)
    ok, why = strategy.evaluate_entry(coin(**{"15m": mixed}), EXTRAS, "risk_on")
    assert ok is False
    assert "15m" in why


def test_four_hour_veto_still_applies():
    ok, why = strategy.evaluate_entry(coin(**{"4H": down()}), EXTRAS, "risk_on")
    assert ok is False
    assert "4H" in why


def test_one_hour_remains_the_qualifying_timeframe():
    weak = tf(adx=5.0)
    ok, why = strategy.evaluate_entry(coin(**{"1H": weak}), EXTRAS, "risk_on")
    assert ok is False
    assert "adx" in why.lower()


def test_min_atr_pct_is_measured_on_the_one_hour_bar():
    # 15m ATR is tiny but 1H ATR is healthy -> still tradeable.
    thin15 = tf(atr=0.01)
    ok, why = strategy.evaluate_entry(coin(**{"15m": thin15}), EXTRAS, "risk_on")
    assert ok is True, why
    # 1H ATR below the floor -> rejected regardless of a lively 15m.
    thin1h = tf(atr=0.01)
    ok, why = strategy.evaluate_entry(coin(**{"1H": thin1h}), EXTRAS, "risk_on")
    assert ok is False
    assert "atr" in why.lower()


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


def test_exit_stop_uses_price_from_15m():
    pos = {"symbol": "ETH", "entry_price": 100.0, "qty": 1.0, "stop": 94.0,
           "atr": 2.0, "peak": 100.0, "r": 6.0}
    action, _ = strategy.check_exit(pos, tf(close=93.0), tf(atr=2.0), "risk_on", 1.0)
    assert action == "stop"


def test_exit_trail_distance_uses_the_one_hour_atr_not_the_15m_atr():
    # Price is well in profit. A 15m ATR of 0.1 would trail far tighter than
    # the 1H ATR of 2.0; the 1H value must win.
    pos = {"symbol": "ETH", "entry_price": 100.0, "qty": 1.0, "stop": 94.0,
           "atr": 2.0, "peak": 112.0, "r": 6.0}
    _, updated = strategy.check_exit(pos, tf(close=112.0, atr=0.1),
                                     tf(atr=2.0), "risk_on", 3.0)
    expected = 112.0 - config.TRAIL_ATR_MULT * 2.0
    assert updated["stop"] == pytest.approx(expected, rel=1e-9)


def test_time_stop_fires_on_wall_clock_hours_not_bar_count():
    pos = {"symbol": "ETH", "entry_price": 100.0, "qty": 1.0, "stop": 94.0,
           "atr": 2.0, "peak": 101.0, "r": 6.0}
    action, _ = strategy.check_exit(pos, tf(close=100.5), tf(atr=2.0),
                                    "risk_on", config.TIME_STOP_HOURS + 1)
    assert action == "time_stop"


def test_btc_is_never_an_entry_candidate_while_risk_off():
    snap = {"symbols": [{"symbol": "BTC", "status": "ok",
                         "timeframes": {"1D": down(), "1H": tf(), "4H": tf(),
                                        "30m": tf(), "15m": tf()}}]}
    cands, _ = strategy.entry_candidates(snap, {"BTC": EXTRAS}, set(), "risk_off")
    assert cands == []
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_strategy.py -q`
Expected: FAIL — `No module named 'tokocrypto.strategy'`

- [ ] **Step 3: Port strategy.py and apply the two changes**

```bash
cp /root/CryptoIndodaxBot/cryptoindodax/strategy.py /root/TokoCryptoBot/tokocrypto/strategy.py
```

Then edit. In `evaluate_entry`, after the existing 4H veto block, insert:

```python
    # --- timing layer ----------------------------------------------------
    # The rungs above decide WHETHER this coin is tradeable; these two decide
    # WHEN. Confirmation first, then the trigger.
    m30 = _tf(coin, config.CONFIRM_TF)
    if m30 and stack(m30) == "DOWN":
        return False, f"{config.CONFIRM_TF} stack DOWN"
    m15 = _tf(coin, config.SIGNAL_TF)
    if not m15:
        return False, f"no {config.SIGNAL_TF} data"
    if stack(m15) != "UP":
        return False, f"{config.SIGNAL_TF} stack {stack(m15)}"
```

Replace every literal `"1H"` with `config.QUALIFY_TF`, `"4H"` with `config.VETO_TF`, and `"1D"` with `config.REGIME_TF`, so the cascade is declared in one place.

Change `check_exit`'s signature and its first lines:

```python
def check_exit(position, signal_tf, anchor_tf, reg, hours_held):
    """Exit decision for one open position.

    `signal_tf` is the 15m block and supplies PRICE — this is what buys the
    faster reaction. `anchor_tf` is the 1H block and supplies ATR — this is what
    keeps 1R the same width it was when the strategy was validated.

    Sizing R off a 15m ATR would cut stop distance to roughly a third, and since
    fee drag is a fraction of 1R, it would push drag from ~31% of 1R to near
    90%. The split signature exists to make that mistake hard to make.
    """
    close = signal_tf["last_close"]
    atr = anchor_tf.get("atr14")
```

Leave the rest of the function body as-is — it already reads `close` and `atr` from locals.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_strategy.py -q`
Expected: PASS (16 tests). If `test_exit_trail_distance_uses_the_one_hour_atr_not_the_15m_atr` fails, `check_exit` is still reading ATR from the signal block — that is the bug this whole task exists to prevent.

- [ ] **Step 5: Commit**

```bash
git add tokocrypto/strategy.py tests/test_strategy.py
git commit -m "feat: add 30m confirmation and 15m trigger beneath the cascade

Exits react on 15m closes while 1R stays anchored to the 1H ATR. The
split check_exit signature makes the anchor explicit rather than
incidental.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Risk, ledger, and policy

**Files:**
- Create: `tokocrypto/risk.py`, `tokocrypto/ledger.py`, `tokocrypto/policy.py`
- Test: `tests/test_risk_ledger_policy.py`

**Interfaces:**
- Consumes: `config.RISK_PCT`, `config.STOP_ATR_MULT`, `config.MIN_ORDER_USDT`, `symbols.round_qty`, `symbols.meets_minimums`
- Produces: `risk.position_size(equity, entry_price, atr, half=False, sym_pair=None) -> float`, `risk.sizing_reason(...) -> str`, `risk.circuit_breaker_tripped(closed_trades, equity, now=None) -> bool`, `ledger.load/save/open_position/close_position/update_position/hours_held/throttled/record_entry_attempt/reconcile`, `policy.load(path=None, now=None) -> dict`

Port all three from CryptoIndodaxBot. `policy.py` is verbatim. `risk.py` and `ledger.py` need IDR→USDT renames and a tax term.

- [ ] **Step 1: Copy the three modules**

```bash
cd /root/TokoCryptoBot
for m in risk ledger policy; do cp /root/CryptoIndodaxBot/cryptoindodax/$m.py tokocrypto/; done
sed -i 's/from \. import config, pairs/from . import config, symbols/; s/\bpairs\./symbols./g; s/MIN_ORDER_IDR/MIN_ORDER_USDT/g; s/DUST_IDR/DUST_USDT/g; s/min_order_idr/min_notional/g' tokocrypto/risk.py tokocrypto/ledger.py
```

Read both files afterwards and fix anything the `sed` missed — particularly `symbols.round_qty`/`meets_minimums` now take a **pair** (`"BTC_USDT"`), not a bare coin symbol. Every call site must pass `config.pair(sym)`.

- [ ] **Step 2: Write the failing fee test**

`ledger._modelled_fee` currently models a single taker fee. Tokocrypto charges tax on top, and CryptoAutoBot lost months of reporting accuracy to a missing fee term.

```python
# tests/test_risk_ledger_policy.py
import pytest
from tokocrypto import config, ledger, risk


def test_modelled_fee_includes_tax_on_top_of_commission():
    notional = 1000.0
    fee = ledger._modelled_fee("BTC_USDT", notional)
    expected = notional * (config.TAKER_FEE_PCT + config.TAX_PCT)
    assert fee == pytest.approx(expected, rel=1e-9)


def test_close_position_reports_net_not_gross():
    led = {"open": [], "closed": [], "last_entry": {}}
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    ledger.close_position(led, pos, exit_price=110.0, reason="tp")
    trade = led["closed"][-1]
    gross = 10.0
    assert trade["pnl"] < gross, "fees and tax must be deducted from the headline P&L"


def test_realised_fee_overrides_the_model_when_the_exchange_reports_one():
    led = {"open": [], "closed": [], "last_entry": {}}
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    ledger.close_position(led, pos, exit_price=110.0, reason="tp", exit_fee=3.0)
    assert led["closed"][-1]["exit_fee"] == 3.0


def test_profit_r_is_recorded_for_the_stop_doctrine():
    led = {"open": [], "closed": [], "last_entry": {}}
    pos = ledger.open_position(led, "BTC", qty=1.0, entry_price=100.0,
                               atr=2.0, order_id="1")
    ledger.close_position(led, pos, exit_price=112.0, reason="tp")
    assert "profit_r" in led["closed"][-1]


def test_position_size_risks_the_configured_fraction():
    # equity 10000, risk 1.5% = 150; stop distance = 3 * ATR 2.0 = 6.0
    qty = risk.position_size(10_000.0, 100.0, 2.0, sym_pair="BTC_USDT")
    assert qty == pytest.approx(150.0 / 6.0, rel=0.02)


def test_position_size_returns_zero_below_the_notional_floor():
    assert risk.position_size(20.0, 100.0, 2.0, sym_pair="BTC_USDT") == 0.0


def test_circuit_breaker_trips_on_rolling_24h_loss():
    closed = [{"pnl": -500.0, "closed_at": "2026-09-19T01:00:00+00:00"}]
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    assert risk.circuit_breaker_tripped(closed, 10_000.0, now=now) is True
```

- [ ] **Step 3: Run and watch the fee test fail**

Run: `.venv/bin/python -m pytest tests/test_risk_ledger_policy.py -q`
Expected: FAIL on `test_modelled_fee_includes_tax_on_top_of_commission`

- [ ] **Step 4: Add the tax term**

In `tokocrypto/ledger.py`:

```python
def _modelled_fee(sym_pair, notional):
    """Commission plus Indonesian withholding.

    Tokocrypto reports `commission` and `taxAmount` separately. Modelling only
    the commission is how CryptoAutoBot reported gross P&L as net for months
    (IMP-B01: a $463 gap, 22% of gross profit). Both terms belong here, and a
    realised fee from the exchange always overrides this estimate.
    """
    return float(notional) * (config.TAKER_FEE_PCT + config.TAX_PCT)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_risk_ledger_policy.py -q`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add tokocrypto/risk.py tokocrypto/ledger.py tokocrypto/policy.py tests/test_risk_ledger_policy.py
git commit -m "feat: port risk, ledger and policy with a tax-aware fee model

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Broker — the Tokocrypto MBX client

**Files:**
- Create: `tokocrypto/broker.py`
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: `config.TOKO_KEY`, `config.TOKO_SECRET`, `config.OPEN_BASE_URL`, `config.RECV_WINDOW_MS`, `symbols.round_qty`
- Produces: `broker.BrokerError`, `broker.get_asset(asset) -> dict`, `broker.get_account(price_by_symbol=None) -> dict`, `broker.get_positions(price_by_symbol=None) -> list`, `broker.market_buy_quote(sym_pair, usdt_amount, client_id=None) -> str`, `broker.market_sell_qty(sym_pair, qty, client_id=None) -> str`, `broker.get_order(order_id) -> dict`, `broker.fill_summary(sym_pair, order_id) -> dict`, `broker.wait_for_fill(sym_pair, order_id, timeout_s=90) -> dict`

Endpoints (all verified against the live docs on 2026-09-19):

| Purpose | Call |
| --- | --- |
| Balance for one asset | `GET /open/v1/account/spot/asset?asset=BTC` |
| New order | `POST /open/v1/orders` — `side` 0=BUY 1=SELL, `type` 2=MARKET |
| Order status | `GET /open/v1/orders/detail?orderId=` |
| Fills | `GET /open/v1/orders/trades?symbol=BTC_USDT` |
| Cancel | `POST /open/v1/orders/cancel` |
| Server time | `GET /open/v1/common/time` |

Order status enum: `-2` SYSTEM_PROCESSING, `0` NEW, `1` PARTIALLY_FILLED, `2` FILLED, `3` CANCELED, `5` REJECTED, `6` EXPIRED.

There is **no bulk balance endpoint** — `/open/v1/account/spot/asset` takes one asset. Query USDT plus each open position only, never the whole watchlist.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_broker.py
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


def test_signature_is_hmac_sha256_over_the_sent_query_string(monkeypatch):
    seen = {}

    def fake_request(method, url, headers=None, timeout=None):
        seen.update(method=method, url=url, headers=headers)
        return _FakeResp({"code": 0, "data": {}})

    monkeypatch.setattr(broker.requests, "request", fake_request)
    broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    qs = seen["url"].split("?", 1)[1]
    sent, sig = qs.rsplit("&signature=", 1)
    expected = hmac.new(b"SECRET456", sent.encode(), hashlib.sha256).hexdigest()
    assert sig == expected
    assert seen["headers"]["X-MBX-APIKEY"] == "KEY123"


def test_request_includes_timestamp_and_recv_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, url, **k: (seen.update(url=url),
                                             _FakeResp({"code": 0, "data": {}}))[1])
    broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    q = urllib.parse.parse_qs(seen["url"].split("?", 1)[1])
    assert "timestamp" in q
    assert q["recvWindow"] == [str(config.RECV_WINDOW_MS)]


def test_nonzero_code_raises_brokererror(monkeypatch):
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: _FakeResp({"code": -2015,
                                                   "msg": "Invalid API-key, IP, or permissions"}))
    with pytest.raises(broker.BrokerError) as e:
        broker._request("GET", "/open/v1/account/spot/asset", {"asset": "BTC"})
    assert "-2015" in str(e.value)


def test_missing_credentials_raises_before_any_network_call(monkeypatch):
    monkeypatch.setattr(config, "TOKO_KEY", None)
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(broker.BrokerError):
        broker.get_asset("BTC")


def test_market_buy_sends_quote_order_qty_and_side_zero(monkeypatch):
    seen = {}
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, url, **k: (seen.update(url=url),
                                             _FakeResp({"code": 0,
                                                        "data": {"orderId": "77"}}))[1])
    oid = broker.market_buy_quote("BTC_USDT", 50.0)
    q = urllib.parse.parse_qs(seen["url"].split("?", 1)[1])
    assert q["side"] == ["0"] and q["type"] == ["2"]
    assert q["quoteOrderQty"] == ["50.0"]
    assert "quantity" not in q
    assert oid == "77"


def test_market_sell_sends_quantity_and_side_one(monkeypatch):
    seen = {}
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, url, **k: (seen.update(url=url),
                                             _FakeResp({"code": 0,
                                                        "data": {"orderId": "78"}}))[1])
    broker.market_sell_qty("BTC_USDT", 0.5)
    q = urllib.parse.parse_qs(seen["url"].split("?", 1)[1])
    assert q["side"] == ["1"] and q["type"] == ["2"]
    assert q["quantity"] == ["0.5"]
    assert "quoteOrderQty" not in q


def test_fill_summary_sums_commission_and_tax(monkeypatch):
    fills = {"code": 0, "data": {"list": [
        {"tradeId": "1", "orderId": "77", "symbol": "BTC_USDT", "price": "100.0",
         "qty": "0.3", "quoteQty": "30.0", "commission": "0.03",
         "commissionAsset": "USDT", "taxAmount": "0.063", "taxRate": "0.0021",
         "isBuyer": 1, "isMaker": 0},
        {"tradeId": "2", "orderId": "77", "symbol": "BTC_USDT", "price": "102.0",
         "qty": "0.2", "quoteQty": "20.4", "commission": "0.02",
         "commissionAsset": "USDT", "taxAmount": "0.042", "taxRate": "0.0021",
         "isBuyer": 1, "isMaker": 0},
    ]}}
    monkeypatch.setattr(broker.requests, "request", lambda *a, **k: _FakeResp(fills))
    s = broker.fill_summary("BTC_USDT", "77")
    assert s["qty"] == pytest.approx(0.5)
    assert s["price"] == pytest.approx(50.4 / 0.5)          # quote-weighted
    assert s["commission"] == pytest.approx(0.05 + 0.105)   # fee AND tax
    assert len(s["fills"]) == 2


def test_fill_summary_ignores_other_orders_fills(monkeypatch):
    fills = {"code": 0, "data": {"list": [
        {"tradeId": "9", "orderId": "OTHER", "symbol": "BTC_USDT", "price": "1.0",
         "qty": "99.0", "quoteQty": "99.0", "commission": "0", "taxAmount": "0"},
    ]}}
    monkeypatch.setattr(broker.requests, "request", lambda *a, **k: _FakeResp(fills))
    assert broker.fill_summary("BTC_USDT", "77")["qty"] == 0.0


def test_wait_for_fill_returns_once_status_is_filled(monkeypatch):
    states = iter([{"code": 0, "data": {"orderId": "77", "status": 0}},
                   {"code": 0, "data": {"orderId": "77", "status": 2,
                                        "executedQty": "0.5"}}])
    monkeypatch.setattr(broker.requests, "request", lambda *a, **k: _FakeResp(next(states)))
    out = broker.wait_for_fill("BTC_USDT", "77", timeout_s=30, sleep=lambda s: None)
    assert out["status"] == 2


def test_wait_for_fill_gives_up_on_rejected(monkeypatch):
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **k: _FakeResp({"code": 0,
                                                   "data": {"orderId": "77", "status": 5}}))
    out = broker.wait_for_fill("BTC_USDT", "77", timeout_s=5, sleep=lambda s: None)
    assert out["status"] == 5


def test_get_positions_skips_dust(monkeypatch):
    monkeypatch.setattr(broker, "get_asset",
                        lambda a, **k: {"asset": a, "free": "0.00000001", "locked": "0"})
    out = broker.get_positions({"BTC": 100_000.0}, assets=["BTC"])
    assert out == []      # $0.001 is dust, below DUST_USDT
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_broker.py -q`
Expected: FAIL — `No module named 'tokocrypto.broker'`

- [ ] **Step 3: Implement broker.py**

Use `/root/CryptoIndodaxBot/cryptoindodax/broker.py` as the structural template — `get_positions`, `wait_for_fill` and `fill_summary` keep the same shape. Replace the transport:

```python
"""Tokocrypto signed REST client (MBX-compatible).

Differences from the Indodax TAPI client this replaces:

  * Auth is HMAC-SHA256 over the urlencoded parameter string, appended as
    `&signature=`, with the key in `X-MBX-APIKEY`. Both GET and POST carry
    everything in the query string — the docs permit it for both, and using one
    form removes a class of signing bug.
  * `side` is 0=BUY / 1=SELL and `type` is an int (2=MARKET), not a string.
  * There is NO bulk balance endpoint. /open/v1/account/spot/asset takes one
    asset, so query USDT plus open positions only — never the whole watchlist.
  * Fills report `commission` AND `taxAmount` separately. Both are cost.
    Summing only the commission understates every trade.
  * Market BUY is sized in USDT (`quoteOrderQty`); market SELL is sized in coin
    (`quantity`). That asymmetry is why buy and sell have separate entrypoints.

Tokocrypto has no sandbox. Every call here moves real money.
"""
import hashlib
import hmac
import time
import urllib.parse

import requests

from . import config, net, symbols


class BrokerError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()

FILLED, CANCELED, REJECTED, EXPIRED = 2, 3, 5, 6
TERMINAL = {FILLED, CANCELED, REJECTED, EXPIRED}


def _credentials():
    if not (config.TOKO_KEY and config.TOKO_SECRET):
        raise BrokerError("Tokocrypto credentials not configured "
                          "(TOKOCRYPTO_API_KEY/TOKOCRYPTO_API_SECRET)")
    return config.TOKO_KEY, config.TOKO_SECRET


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _request(method, path, params=None, session=None):
    """Signed call. Returns the `data` block, raising on a non-zero code."""
    key, secret = _credentials()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = config.RECV_WINDOW_MS
    qs = urllib.parse.urlencode(params)
    url = f"{config.OPEN_BASE_URL}{path}?{qs}&signature={_sign(secret, qs)}"
    caller = (session or requests).request
    try:
        r = caller(method, url,
                   headers={"X-MBX-APIKEY": key, "User-Agent": config.USER_AGENT},
                   timeout=20)
        r.raise_for_status()
        body = r.json()
    except BrokerError:
        raise
    except Exception as e:
        raise BrokerError(f"{method} {path}: {e}") from e
    if body.get("code") not in (0, None):
        raise BrokerError(f"{method} {path}: {body.get('msg') or body.get('message')} "
                          f"(code {body.get('code')})")
    return body.get("data") or {}


def get_asset(asset, session=None):
    return _request("GET", "/open/v1/account/spot/asset", {"asset": asset},
                    session=session)


def get_positions(price_by_symbol=None, assets=None, session=None):
    """Spot balances worth more than DUST_USDT, as position dicts.

    A spot balance carries no entry price, so the ledger — not the exchange —
    remains the source of truth for what a position cost.
    """
    price_by_symbol = price_by_symbol or {}
    out = []
    for asset in (assets if assets is not None else list(price_by_symbol)):
        try:
            bal = get_asset(asset, session=session)
        except BrokerError:
            continue
        qty = float(bal.get("free") or 0) + float(bal.get("locked") or 0)
        price = price_by_symbol.get(asset)
        if not qty or not price or qty * price < config.DUST_USDT:
            continue
        out.append({"symbol": asset, "qty": qty,
                    "free": float(bal.get("free") or 0),
                    "value": qty * price})
    return out


def get_account(price_by_symbol=None, session=None):
    """Equity in USDT: the cash balance plus the value of held coins."""
    cash = float(get_asset(config.QUOTE, session=session).get("free") or 0)
    positions = get_positions(price_by_symbol, session=session)
    return {"cash": cash,
            "positions_value": sum(p["value"] for p in positions),
            "equity": cash + sum(p["value"] for p in positions)}


def _order_id(data):
    oid = data.get("orderId")
    if oid is None:
        raise BrokerError(f"order response carried no orderId: {data!r:.160}")
    return str(oid)


def market_buy_quote(sym_pair, usdt_amount, client_id=None, session=None):
    """Market BUY sized in USDT."""
    params = {"symbol": sym_pair, "side": 0, "type": 2,
              "quoteOrderQty": str(usdt_amount)}
    if client_id:
        params["clientId"] = client_id
    return _order_id(_request("POST", "/open/v1/orders", params, session=session))


def market_sell_qty(sym_pair, qty, client_id=None, session=None):
    """Market SELL sized in coin."""
    params = {"symbol": sym_pair, "side": 1, "type": 2, "quantity": str(qty)}
    if client_id:
        params["clientId"] = client_id
    return _order_id(_request("POST", "/open/v1/orders", params, session=session))


def get_order(order_id, session=None):
    return _request("GET", "/open/v1/orders/detail", {"orderId": order_id},
                    session=session)


def cancel_order(order_id, session=None):
    return _request("POST", "/open/v1/orders/cancel", {"orderId": order_id},
                    session=session)


def fill_summary(sym_pair, order_id, limit=100, session=None):
    """Quantity, quote-weighted average price, and TOTAL cost for one order.

    `commission` is the exchange fee; `taxAmount` is Indonesian withholding
    charged on top. Both are money leaving the account, so both are summed into
    `commission`. Reporting only the fee is the bug that made CryptoAutoBot
    overstate profit by 22% for months.
    """
    data = _request("GET", "/open/v1/orders/trades",
                    {"symbol": sym_pair, "limit": limit}, session=session)
    mine = [f for f in (data.get("list") or []) if str(f.get("orderId")) == str(order_id)]
    qty = sum(float(f.get("qty") or 0) for f in mine)
    quote = sum(float(f.get("quoteQty") or 0) for f in mine)
    cost = sum(float(f.get("commission") or 0) + float(f.get("taxAmount") or 0)
               for f in mine)
    return {"qty": qty,
            "price": (quote / qty) if qty else None,
            "quote": quote,
            "commission": cost,
            "fills": mine}


def wait_for_fill(sym_pair, order_id, timeout_s=90, poll_s=3, sleep=time.sleep,
                  session=None):
    """Poll until the order reaches a terminal status or the timeout expires."""
    deadline = time.time() + timeout_s
    order = {}
    while time.time() < deadline:
        order = get_order(order_id, session=session)
        if order.get("status") in TERMINAL:
            return order
        sleep(poll_s)
    return order


def close_position(sym_pair, qty, session=None):
    """Exit is a market SELL — there is no close-position endpoint.

    Round to the symbol's step size first: fees are taken in-asset, so the held
    quantity drifts below the fill and an unrounded sell is rejected.
    """
    return market_sell_qty(sym_pair, symbols.round_qty(sym_pair, qty), session=session)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_broker.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Verify signing against the live API without credentials**

```bash
.venv/bin/python -c "
from tokocrypto import broker
try:
    broker.get_asset('USDT')
except broker.BrokerError as e:
    print('expected:', e)
"
```
Expected: `Tokocrypto credentials not configured`. It must fail *before* any network call — if you see an HTTP error instead, `_credentials()` is being checked too late.

- [ ] **Step 6: Commit**

```bash
git add tokocrypto/broker.py tests/test_broker.py
git commit -m "feat: Tokocrypto MBX broker client with tax-inclusive fill costs

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Telegram and the order lock

**Files:**
- Create: `tokocrypto/notify.py`, `tokocrypto/lock.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `config.TELEGRAM_TOKEN`, `config.TELEGRAM_CHAT_IDS`
- Produces: `notify.send(text) -> bool`, `notify.send_to(chat_id, text) -> bool`, `notify.resolve_chat_id()`, `lock.held(wait_s=0.0, ...)` context manager

- [ ] **Step 1: Port both modules**

```bash
cd /root/TokoCryptoBot
cp /root/CryptoIndodaxBot/cryptoindodax/notify.py tokocrypto/
cp /root/CryptoIndodaxBot/cryptoindodax/lock.py tokocrypto/
cp /root/CryptoIndodaxBot/tests/test_notify.py tests/
sed -i 's/cryptoindodax/tokocrypto/g' tests/test_notify.py
sed -i 's/CryptoIndodaxBot/TokoCryptoBot/g' tokocrypto/notify.py tokocrypto/lock.py
```

`notify.py` already fans out to a comma-separated `TELEGRAM_CHAT_ID` with per-chat failure isolation — one blocked chat never stops the others. Keep that behaviour.

Update `lock.py`'s docstring: the overlap it guards is now between the 15-minute trader and its own previous run, not an hourly trader and a 5-minute watcher. The mechanism is unchanged — the trader **waits** for the lock rather than skipping, because a skipped cycle suspends exits and the circuit breaker.

- [ ] **Step 2: Run the ported tests**

Run: `.venv/bin/python -m pytest tests/test_notify.py -q`
Expected: PASS

- [ ] **Step 3: Send a live message to confirm the wiring**

```bash
.venv/bin/python -c "
from tokocrypto import notify
print('delivered:', notify.send('TokoCryptoBot: notify module wired up.'))
"
```
Expected: `delivered: True` and the message arrives in Telegram.

- [ ] **Step 4: Commit**

```bash
git add tokocrypto/notify.py tokocrypto/lock.py tests/test_notify.py
git commit -m "feat: port Telegram fan-out and the order lock

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: The trader cycle

**Files:**
- Create: `tokocrypto/trader.py`
- Test: `tests/test_trader.py`

**Interfaces:**
- Consumes: everything above
- Produces: `trader.load_current_snapshot(now=None) -> dict|None`, `trader.fetch_extras(symbols, now=None) -> dict`, `trader.run(dry_run=False, now=None)`

Port `/root/CryptoIndodaxBot/cryptoindodax/trader.py`. Order of operations is unchanged: load snapshot → reconcile with balances → exits → circuit breaker → entries → persist ledger + notify.

**Three changes:**

1. `load_current_snapshot` globs `*-*.json` and enforces `SNAPSHOT_MAX_AGE_MIN = 20`.
2. Exit checks pass **two** timeframe blocks to `strategy.check_exit` — the 15m block for price, the 1H block for ATR.
3. `_enter_position` sizes market buys in USDT via `broker.market_buy_quote`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trader.py
import json
from datetime import datetime, timedelta, timezone

import pytest
from tokocrypto import config, trader


def _write_snap(tmp_path, when, captured_at=None):
    d = tmp_path / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{when.strftime('%H-%M')}.json"
    p.write_text(json.dumps({"captured_at": (captured_at or when).isoformat(),
                             "symbols": []}))
    return p


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


def test_exit_check_passes_signal_and_anchor_timeframes(monkeypatch):
    """The 15m block supplies price, the 1H block supplies ATR."""
    seen = {}

    def fake_check_exit(position, signal_tf, anchor_tf, reg, hours_held):
        seen.update(signal=signal_tf, anchor=anchor_tf)
        return None, position

    monkeypatch.setattr(trader.strategy, "check_exit", fake_check_exit)
    coin = {"symbol": "ETH", "timeframes": {
        "15m": {"status": "ok", "last_close": 10.0, "atr14": 0.1},
        "1H": {"status": "ok", "last_close": 10.0, "atr14": 2.0}}}
    snap = {"captured_at": "2026-09-19T14:32:00+00:00", "symbols": [coin]}
    pos = {"symbol": "ETH", "entry_price": 9.0, "qty": 1.0, "stop": 8.0,
           "atr": 2.0, "peak": 10.0, "r": 1.0}
    trader.check_one_exit(snap, pos, "risk_on", 1.0)
    assert seen["signal"]["atr14"] == 0.1      # 15m block
    assert seen["anchor"]["atr14"] == 2.0      # 1H block — the R anchor


def test_dry_run_places_no_orders(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(trader.broker, "market_buy_quote",
                        lambda *a, **k: pytest.fail("dry run must not place orders"))
    monkeypatch.setattr(trader.broker, "market_sell_qty",
                        lambda *a, **k: pytest.fail("dry run must not place orders"))
    now = datetime(2026, 9, 19, 14, 36, tzinfo=timezone.utc)
    _write_snap(tmp_path, datetime(2026, 9, 19, 14, 32, tzinfo=timezone.utc))
    trader.run(dry_run=True, now=now)


def test_trading_disabled_blocks_the_cycle(monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    monkeypatch.setattr(trader.broker, "market_buy_quote",
                        lambda *a, **k: pytest.fail("must not trade while disabled"))
    trader.run(dry_run=False)
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_trader.py -q`
Expected: FAIL — `No module named 'tokocrypto.trader'`

- [ ] **Step 3: Port trader.py and apply the three changes**

```bash
cp /root/CryptoIndodaxBot/cryptoindodax/trader.py /root/TokoCryptoBot/tokocrypto/trader.py
sed -i 's/cryptoindodax/tokocrypto/g; s/CryptoIndodaxBot/TokoCryptoBot/g; s/\bpairs\b/symbols/g; s/fmt_idr/fmt_usdt/g' /root/TokoCryptoBot/tokocrypto/trader.py
```

Then apply by hand:

```python
def load_current_snapshot(now=None):
    """Latest snapshot no older than SNAPSHOT_MAX_AGE_MIN, else None.

    At a 15-minute cadence a stale snapshot is far more dangerous than it was
    hourly: the budget is 20 minutes, so a missed capture halts trading within
    one cycle instead of silently trading hour-old indicators.
    """
    now = now or datetime.now(timezone.utc)
    day = config.DATA_DIR / now.strftime("%Y-%m-%d")
    files = sorted(day.glob("*-*.json"))
    if not files:
        return None
    snap = json.loads(files[-1].read_text())
    age = now - datetime.fromisoformat(snap["captured_at"])
    if age > timedelta(minutes=config.SNAPSHOT_MAX_AGE_MIN):
        return None
    return snap


def _coin_block(snap, symbol, tf_key):
    for c in snap.get("symbols", []):
        if c.get("symbol") == symbol:
            block = (c.get("timeframes") or {}).get(tf_key)
            return block if block and block.get("status") == "ok" else None
    return None


def check_one_exit(snap, pos, reg, hours_held):
    """Evaluate one position. Price comes from 15m, ATR from 1H."""
    signal = _coin_block(snap, pos["symbol"], config.SIGNAL_TF)
    anchor = _coin_block(snap, pos["symbol"], config.ATR_ANCHOR_TF)
    if not signal or not anchor:
        return None, pos
    return strategy.check_exit(pos, signal, anchor, reg, hours_held)
```

Replace the exit loop body in `_run` with a call to `check_one_exit`, and change the entry order call from `broker.market_buy_idr(...)` to:

```python
        order_id = broker.market_buy_quote(config.pair(sym), notional)
```

where `notional = qty * price` rounded to 2 decimals.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_trader.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Run a real dry-run cycle**

```bash
.venv/bin/python -m tokocrypto.snapshot
.venv/bin/python -m tokocrypto.trader --dry-run
```
Expected: logs a regime, evaluates every watchlist coin, prints accept/reject reasons, places nothing. Rejections naming `15m stack` or `30m stack DOWN` confirm the timing layer is live.

- [ ] **Step 6: Run the whole suite and commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add tokocrypto/trader.py tests/test_trader.py
git commit -m "feat: 15-minute trader cycle with split signal/anchor exit checks

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Replay harness

**Files:**
- Create: `tokocrypto/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `data.fetch_klines`, `strategy.*`, `risk.*`, `ledger.*`
- Produces: `replay.load_klines(symbols, tf_keys, days) -> dict`, `replay.build_history(klines) -> list`, `replay.run(history, start_equity=1000.0, fee_pct=None, **overrides) -> dict`, `replay.summarize(result) -> str`, `replay.buy_and_hold(history, symbol="BTC", start_equity=1000.0) -> float`, `replay.main(argv=None)`

This is the go-live gate. Unlike CryptoIndodaxBot's replay — which could only read accumulated hourly snapshots — this one reconstructs history from klines, so it works on day one.

**Critical:** the replay must drive the **real** `strategy` and `risk` functions, never a reimplementation. A backtest that reimplements the strategy tests the reimplementation.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replay.py
import pytest
from tokocrypto import config, replay


def _synthetic(n=400, start=100.0, drift=0.25):
    """A steadily rising series, so a long-only trend strategy should profit."""
    bars = []
    for i in range(n):
        c = start + drift * i
        bars.append({"t": f"2026-09-{(i // 96) + 1:02d}T00:00:00+00:00",
                     "o": c - 0.1, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 1000.0})
    return bars


def test_history_frames_align_timeframes_to_the_signal_clock():
    klines = {"BTC": {tf: _synthetic() for tf in config.TIMEFRAMES}}
    hist = replay.build_history(klines)
    assert hist, "history must not be empty"
    frame = hist[0]
    assert "captured_at" in frame and "symbols" in frame
    tfs = frame["symbols"][0]["timeframes"]
    assert set(tfs) == set(config.TIMEFRAMES)


def test_run_reports_the_stop_doctrine_buckets():
    klines = {"BTC": {tf: _synthetic() for tf in config.TIMEFRAMES}}
    result = replay.run(replay.build_history(klines), start_equity=1000.0)
    for key in ("win", "scratch", "fail", "stop_rate", "true_win_rate",
                "trades", "end_equity", "profit_factor"):
        assert key in result, f"missing {key}"


def test_buckets_are_computed_from_profit_r_not_pnl_sign():
    """A +0.0% stop-out is a FAIL, not a win. Standing doctrine."""
    trades = [{"pnl": 0.01, "profit_r": 0.002, "reason": "stop"},
              {"pnl": -5.0, "profit_r": -1.0, "reason": "stop"},
              {"pnl": 30.0, "profit_r": 2.5, "reason": "tp"}]
    b = replay.bucket(trades)
    assert b["win"] == 1
    assert b["fail"] == 2          # both stops fail, including the flat one
    assert b["stop_rate"] == pytest.approx(2 / 3)


def test_fee_pct_override_reduces_net_profit():
    klines = {"BTC": {tf: _synthetic() for tf in config.TIMEFRAMES}}
    hist = replay.build_history(klines)
    cheap = replay.run(hist, start_equity=1000.0, fee_pct=0.0)
    dear = replay.run(hist, start_equity=1000.0, fee_pct=0.02)
    assert dear["end_equity"] < cheap["end_equity"]


def test_run_uses_the_real_strategy_module(monkeypatch):
    called = {"n": 0}
    real = replay.strategy.evaluate_entry

    def counting(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(replay.strategy, "evaluate_entry", counting)
    klines = {"BTC": {tf: _synthetic() for tf in config.TIMEFRAMES}}
    replay.run(replay.build_history(klines), start_equity=1000.0)
    assert called["n"] > 0, "replay must drive the real strategy, not a copy"
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_replay.py -q`
Expected: FAIL — `No module named 'tokocrypto.replay'`

- [ ] **Step 3: Implement replay.py**

Model the cycle loop on `/root/CryptoIndodaxBot/cryptoindodax/replay.py` `run()` — regime → exits → circuit breaker → entries, calling the real `strategy` and `risk`. New pieces:

```python
def load_klines(symbols=None, tf_keys=None, days=10, session=None):
    """Fetch raw klines per symbol per timeframe, oldest first."""
    symbols = symbols or config.WATCHLIST
    tf_keys = tf_keys or list(config.TIMEFRAMES)
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    out = {}
    for sym in symbols:
        out[sym] = {}
        for tf in tf_keys:
            out[sym][tf] = data.fetch_klines(
                config.chart(sym), config.TIMEFRAMES[tf],
                limit=1000, start=start, end=end, session=session)
    return out


def build_history(klines):
    """Replay frames on the 15m clock, each carrying every timeframe's
    indicators AS OF that moment.

    The subtle failure here is lookahead: a 1H block computed from bars that
    close after the frame's timestamp would let the backtest see the future and
    report an edge that cannot exist live. Each timeframe is therefore truncated
    to bars whose close time is <= the frame timestamp.
    """
    frames = []
    signal_bars = next(iter(klines.values()))[config.SIGNAL_TF]
    warmup = 60
    for i in range(warmup, len(signal_bars)):
        ts = signal_bars[i]["t"]
        frame = {"captured_at": ts, "symbols": []}
        for sym, by_tf in klines.items():
            tfs = {}
            for tf, bars in by_tf.items():
                visible = [b for b in bars if b["t"] <= ts]
                ind = snapshot.compute_for_bars(visible)
                tfs[tf] = {"status": "no_data"} if ind is None else {"status": "ok", **ind}
            frame["symbols"].append(
                {"symbol": sym, "pair": config.pair(sym), "status": "ok",
                 "timeframes": tfs})
        frames.append(frame)
    return frames


def bucket(trades):
    """WIN / SCRATCH / FAIL off profit_R — never off the sign of P&L.

    Standing doctrine: any stop-triggered exit is a FAILURE whatever the P&L
    sign. A +0.0% break-even stop is not a win. Reporting `pnl > 0` as the win
    rate overstated USTradeBot's by 90%.
    """
    win = sum(1 for t in trades if t.get("reason") != "stop"
              and (t.get("profit_r") or 0) >= 1.0)
    fail = sum(1 for t in trades if t.get("reason") == "stop")
    scratch = len(trades) - win - fail
    n = len(trades) or 1
    return {"win": win, "scratch": scratch, "fail": fail,
            "stop_rate": fail / n, "true_win_rate": win / n}
```

`run()` returns the bucket keys merged with `trades`, `end_equity`, `profit_factor`, `max_drawdown`, `payoff`.

`summarize()` renders headline, then stop rate beside true win rate, then the buy-and-hold comparison — and when `true_win_rate` is low while `stop_rate` is high, it prints **"NO DEMONSTRATED EDGE"** rather than suggesting a knob.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_replay.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the first real backtest**

```bash
.venv/bin/python -m tokocrypto.replay --days 10 | tee /tmp/first-backtest.txt
```
Record the output verbatim. This is the go-live gate — a losing result is a valid and expected outcome, not a prompt to tune. CryptoAutoBot's IMP-B03 produced exactly that and shipped no config.

- [ ] **Step 6: Commit**

```bash
git add tokocrypto/replay.py tests/test_replay.py
git commit -m "feat: kline-driven replay harness with stop-doctrine reporting

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Scorecard and README

**Files:**
- Create: `tokocrypto/scorecard.py`, `README.md`
- Test: `tests/test_scorecard.py`

**Interfaces:**
- Consumes: `ledger.load`, `replay.bucket`
- Produces: `scorecard.metrics(led=None, equity=None, now=None) -> dict`, `scorecard.render(m) -> str`

- [ ] **Step 1: Port the scorecard**

```bash
cp /root/CryptoIndodaxBot/cryptoindodax/scorecard.py /root/TokoCryptoBot/tokocrypto/
cp /root/CryptoIndodaxBot/tests/test_scorecard.py /root/TokoCryptoBot/tests/
cd /root/TokoCryptoBot
sed -i 's/cryptoindodax/tokocrypto/g; s/fmt_idr/fmt_usdt/g; s/CryptoIndodaxBot/TokoCryptoBot/g' tokocrypto/scorecard.py tests/test_scorecard.py
```

Make `metrics()` delegate its bucketing to `replay.bucket` so the live scorecard and the backtest can never disagree about what a win is.

- [ ] **Step 2: Add the doctrine test**

```python
# append to tests/test_scorecard.py
def test_scorecard_and_replay_agree_on_what_a_win_is():
    from tokocrypto import replay, scorecard
    trades = [{"pnl": 0.01, "profit_r": 0.002, "reason": "stop"},
              {"pnl": 25.0, "profit_r": 2.0, "reason": "tp"}]
    m = scorecard.metrics(led={"open": [], "closed": trades}, equity=1000.0)
    b = replay.bucket(trades)
    assert m["true_win_rate"] == b["true_win_rate"]
    assert m["stop_rate"] == b["stop_rate"]
```

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_scorecard.py -q`
Expected: PASS

- [ ] **Step 4: Write README.md**

Model on `/root/CryptoIndodaxBot/README.md`. Required sections: what it is, the ⚠️ read-before-enabling-trading warning, before-first-run checklist, layout, running commands, **a "What changed from CryptoIndodaxBot" table** (exchange, quote, bars endpoint, auth, symbol spellings, order sizing, fees+tax, minimums, sandbox), and a "Traps worth remembering" section covering the IPv6 whitelist bug, the in-progress candle, the 1H ATR anchor, and "a restart is not a deploy."

- [ ] **Step 5: Commit**

```bash
git add tokocrypto/scorecard.py tests/test_scorecard.py README.md
git commit -m "feat: scorecard sharing the replay's bucketing, plus README

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: Cron installation

**Files:**
- Create: `scripts/install-cron.sh`
- Modify: root crontab

- [ ] **Step 1: Write the install script**

```bash
#!/usr/bin/env bash
# scripts/install-cron.sh — idempotent cron installation for TokoCryptoBot.
#
# Offsets are chosen to stay clear of the sibling bots:
#   CryptoAutoBot     :05 snapshot, :12 trader
#   CryptoIndodaxBot  :07 snapshot, :14 trader
#   TokoCryptoBot     :02/:17/:32/:47 snapshot, :06/:21/:36/:51 trader
set -euo pipefail
ROOT=/root/TokoCryptoBot
MARK="# --- TokoCryptoBot ---"
tmp=$(mktemp)
crontab -l 2>/dev/null | grep -v 'TokoCryptoBot' > "$tmp" || true
cat >> "$tmp" <<EOF
$MARK
2,17,32,47 * * * * cd $ROOT && .venv/bin/python -m tokocrypto.snapshot >> $ROOT/logs/snapshot.log 2>&1
6,21,36,51 * * * * cd $ROOT && .venv/bin/python -m tokocrypto.trader   >> $ROOT/logs/trader.log 2>&1
10 */4 * * * /root/claude-routines/run-routine.sh tokocrypto-review >> /root/claude-routines/logs/cron.log 2>&1
EOF
crontab "$tmp"
rm -f "$tmp"
crontab -l | grep -A4 "$MARK"
```

- [ ] **Step 2: Back up the existing crontab, then install**

```bash
crontab -l > /root/crontab.backup.$(date +%Y%m%d-%H%M%S)
chmod +x scripts/install-cron.sh && ./scripts/install-cron.sh
```

- [ ] **Step 3: Confirm the siblings survived**

```bash
crontab -l | grep -cE 'cryptoauto|cryptoindodax'
```
Expected: the same count as before installing. If it dropped, restore from the backup immediately.

- [ ] **Step 4: Watch one live cycle**

```bash
sleep 900 && tail -20 logs/snapshot.log logs/trader.log
```
Expected: a snapshot written and a trader cycle logged, with `TRADING_ENABLED is false` since no credentials are set.

- [ ] **Step 5: Commit**

```bash
git add scripts/install-cron.sh
git commit -m "feat: cron installation offset clear of the sibling bots

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 14: The 4-hourly self-review routine

**Files:**
- Create: `tokocrypto/review.py`, `/root/claude-routines/prompts/tokocrypto-review.md`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `scorecard.metrics`, `replay.run`, `ledger.load`
- Produces: `review.collect(now=None) -> dict`, `review.render(state) -> str`

The routine runs headless Claude every 4 hours at `:10`. It reviews what exists, re-runs the backtest, reports the verdict to Telegram, and may commit improvements.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review.py
from tokocrypto import review


def test_collect_reports_build_state():
    state = review.collect()
    for key in ("modules", "tests_passing", "test_count", "has_credentials",
                "trading_enabled", "open_positions", "closed_trades",
                "last_snapshot", "git_head"):
        assert key in state


def test_render_leads_with_the_verdict_not_the_headline_pnl():
    state = {"modules": 14, "tests_passing": True, "test_count": 80,
             "has_credentials": False, "trading_enabled": False,
             "open_positions": 0, "closed_trades": 0,
             "last_snapshot": "2026-09-19T14:32:00+00:00", "git_head": "abc1234",
             "backtest": {"verdict": "NO DEMONSTRATED EDGE", "trades": 12,
                          "true_win_rate": 0.08, "stop_rate": 0.75,
                          "end_equity": 940.0, "start_equity": 1000.0,
                          "buy_and_hold": 1060.0}}
    out = review.render(state)
    assert "NO DEMONSTRATED EDGE" in out
    assert "stop rate" in out.lower()
    assert "true win rate" in out.lower()


def test_render_flags_when_backtest_is_missing():
    state = {"modules": 3, "tests_passing": True, "test_count": 20,
             "has_credentials": False, "trading_enabled": False,
             "open_positions": 0, "closed_trades": 0, "last_snapshot": None,
             "git_head": "abc1234", "backtest": None}
    assert "no backtest" in review.render(state).lower()


def test_render_never_claims_live_trading_while_disabled():
    state = {"modules": 14, "tests_passing": True, "test_count": 80,
             "has_credentials": True, "trading_enabled": False,
             "open_positions": 0, "closed_trades": 0,
             "last_snapshot": "2026-09-19T14:32:00+00:00", "git_head": "abc",
             "backtest": None}
    out = review.render(state).lower()
    assert "not trading" in out or "trading disabled" in out
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_review.py -q`
Expected: FAIL — `No module named 'tokocrypto.review'`

- [ ] **Step 3: Implement review.py**

`collect()` gathers: module count in `tokocrypto/`, pytest pass/fail and count (via `subprocess`), whether credentials are set, `TRADING_ENABLED`, ledger open/closed counts, newest snapshot `captured_at`, and `git rev-parse --short HEAD`. `backtest` is `None` when no replay result is cached.

`render()` produces the Telegram body. It leads with the backtest **verdict**, then stop rate beside true win rate, then build state. It must never describe the bot as trading while `trading_enabled` is false.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_review.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the routine prompt**

Create `/root/claude-routines/prompts/tokocrypto-review.md`. Read an existing prompt (e.g. `cryptoindodax-daily`) first to match the house format. The prompt must instruct Claude to:

1. Run `python -m tokocrypto.review` and read the state.
2. Run `python -m pytest tests/ -q`. If red, fix it before anything else.
3. Re-run `python -m tokocrypto.replay --days 10` and compare with the previous verdict recorded in `memory/`.
4. Pick **at most one** improvement, implement it test-first, and commit it as `IMP-NNN`.
5. Push to `origin main`.
6. Send the rendered review to Telegram.

And it must state these constraints explicitly:

- **Never** set `TRADING_ENABLED=true`. Only the user does that.
- **Never** widen, loosen, or remove a stop to improve a metric.
- If the backtest says no demonstrated edge, **report that** — do not tune knobs until it passes. Repeated failure escalates to "retire or rebuild", not to more tuning.
- Record every change in `memory/` as `IMP-NNN` with what changed, why, and the measured before/after.

- [ ] **Step 6: Test the routine end to end**

```bash
/root/claude-routines/run-routine.sh tokocrypto-review
```
Expected: a Telegram message arrives leading with the backtest verdict. Check `/root/claude-routines/logs/cron.log` if nothing appears.

- [ ] **Step 7: Commit and push everything**

```bash
cd /root/TokoCryptoBot
.venv/bin/python -m pytest tests/ -q
git add tokocrypto/review.py tests/test_review.py
git commit -m "feat: 4-hourly self-review reporting the backtest verdict

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push origin main
```

---

## Self-Review

**Spec coverage:** USDT quote → Task 1. Timeframe cascade → Tasks 1, 5, 6. 1H ATR anchor → Tasks 1, 6, 10. Module rewrites → Tasks 3, 4, 8. Ports → Tasks 2, 7, 9, 12. Data layer + rate-limit budget → Tasks 3, 5. Cost model with tax → Tasks 7, 8. Cadence → Tasks 5, 10, 13. Config deltas → Task 1. Replay + go-live gate → Tasks 11, 12. Known traps → Tasks 1 (IPv6), 3 (in-progress candle), 6 (ATR anchor), 8 (in-asset fee drift), 12 (README). Prerequisites → Task 14 prompt. Out-of-scope items are absent as intended.

**Type consistency:** `check_exit(position, signal_tf, anchor_tf, reg, hours_held)` is used identically in Tasks 6 and 10. `symbols.*` take a pair (`"BTC_USDT"`) everywhere — Tasks 4, 7, 8. `config.pair()` vs `config.chart()` used per endpoint family throughout. `replay.bucket` is shared by Tasks 11 and 12.

**Known gap:** `digest.py` and `watchdog.py` from the source repo are deliberately not ported. `digest` summarises hourly snapshots for a daily Claude routine that is out of scope until the core bot is validated; `watchdog` duplicates exit checking that the 15-minute cycle now covers directly. Both are listed as post-validation follow-ups rather than silently dropped.
