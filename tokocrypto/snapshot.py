"""Per-cycle indicator capture across the five-timeframe cascade.

The payload shape matches CryptoIndodaxBot's so replay and any later digest
port unchanged. Two differences: five timeframes rather than three, and the
file is named HH-MM.json because cycles are every 15 minutes.

Tokocrypto rate-limits by IP and answers abuse with a 418 ban escalating from
2 minutes to 3 days. Refetching 4H and 1D every cycle would be three times the
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
        # The previous bar's close, so the trader can derive day-change and
        # "is this close green" without a second round trip per symbol. At four
        # cycles an hour that saved 80 API calls an hour for data the snapshot
        # already held.
        "prev_close": closes[-2] if len(closes) >= 2 else None,
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
        files = sorted(day.glob("*-*.json"))
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
