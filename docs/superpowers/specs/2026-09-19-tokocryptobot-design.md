# TokoCryptoBot — design

**Date:** 2026-09-19
**Repo:** `/root/TokoCryptoBot` → `https://github.com/redowls/TokoCryptoBot.git`
**Lineage:** CryptoAutoBot (Alpaca/USD) → CryptoIndodaxBot (Indodax/IDR) → **TokoCryptoBot (Tokocrypto/USDT)**

## Goal

A fourth sibling in the crypto bot family, trading Tokocrypto instead of Indodax,
with tighter transaction timing: 15-minute and 30-minute candles added to the
timeframe set, and the trading cycle running every 15 minutes instead of hourly.

The strategy, risk model, ledger, policy overlay and stop doctrine are inherited
unchanged. What is new is the exchange adapter and the timing layer.

## Verified before writing this

Everything below was checked against the live API on 2026-09-19, not assumed.

| Claim | Evidence |
| --- | --- |
| Tokocrypto API is alive and Binance-MBX shaped | `GET /open/v1/common/symbols` returns 850 symbols |
| 15m and 30m klines are served | `GET tokocrypto.site/api/v3/klines?interval=15m\|30m` returns live bars |
| All ten watchlist coins exist in USDT | `BTC/ETH/SOL/XRP/DOGE/AVAX/LINK/DOT/LTC/UNI_USDT`, all `type: 1` |
| USDT minimums are small | `minNotional` $5.00 (DOGE $1.00) |
| IDR would have broken the watchlist | only 40 IDR pairs; LINK, DOT and UNI have none |
| The repo exists and is empty | `git ls-remote` returns no refs |

### Why USDT, not IDR

Decided with the user. Tokocrypto lists 466 USDT pairs against 40 IDR pairs.
In IDR the current Indodax watchlist loses three of its five live coins, the
minimum order doubles to Rp20.000, and BTC_IDR shows a 0.088% spread against
0.000% on BTC_USDT. At a 15-minute cadence spread is paid four times as often,
so the tighter book is worth the FX exposure the USDT balance carries.

## Architecture

### The timeframe cascade

CryptoIndodaxBot uses three rungs: 1D sets the BTC regime, 4H vetoes, 1H
produces the entry signal and drives exits. The two new candles are inserted
**below** that cascade as a timing layer, not shifted into it.

| Timeframe | Role | Status |
| --- | --- | --- |
| 1D | BTC regime gate (EMA stack + ADX14) | unchanged |
| 4H | veto — reject if stack is DOWN | unchanged |
| 1H | coin must qualify: EMA stack UP, ADX/RSI/ATR gates | unchanged |
| **30m** | confirmation — reject if stack is DOWN | new |
| **15m** | entry trigger and exit evaluation, every close | new |

A coin still earns its way through the same three filters it does today. What
changes is that entry lands at a 15-minute-precise moment rather than wherever
the hour ticked, and an exit reacts within 15 minutes rather than 60.

**Rejected alternative:** sliding the whole ladder down one rung (15m signal,
30m veto, 1H regime). That discards the calibration the 1D/4H/1H cascade earned
across the Indodax insights, and multiplies entry count — the quantity that
drives fee drag — with nothing holding it back.

### The stop stays anchored to the 1H ATR

This is the decision the project lives or dies on.

`STOP_ATR_MULT = 3.0` against a **15m** ATR would produce a stop roughly a third
the width of today's. Fee drag is measured as a fraction of 1R, so a measured
0.63% round trip that costs 31% of 1R on the 1H clock would cost close to 90% of
1R on the 15m clock. The edge would be arithmetically dead before the strategy
ran a single cycle.

Therefore: **exits evaluate on 15m closes, but `1R` is computed from the 1H
ATR.** The position keeps the R geometry that was validated; only reaction speed
changes. `MIN_ATR_PCT` continues to gate on the 1H ATR for the same reason.

This constraint is load-bearing. Any future change that re-anchors R to a faster
timeframe must re-derive the fee-drag ceiling first, not adjust it afterwards.

## Module map

### Rewritten — Tokocrypto is MBX, not Indodax TAPI

| Module | Change |
| --- | --- |
| `config.py` | endpoints, `QUOTE = "USDT"`, Binance interval codes, USDT minimums |
| `data.py` | `GET /api/v3/klines`; array-of-arrays payload, ms timestamps, `limit` up to 1000 |
| `broker.py` | HMAC-SHA256 over the query string, `X-MBX-APIKEY` header, `side` 0=BUY/1=SELL, `type` 2=MARKET, `quoteOrderQty` for buys and `quantity` for sells |
| `symbols.py` (was `pairs.py`) | `GET /open/v1/common/symbols`; honour `LOT_SIZE.stepSize`, `LOT_SIZE.minQty`, `NOTIONAL.minNotional` |

Symbol spelling differs by endpoint: `/open/v1/*` uses `BTC_USDT`, the kline and
depth hosts want `BTCUSDT`. One helper owns both spellings so no caller guesses.

### Ported near-unchanged

`indicators.py`, `policy.py`, `ledger.py`, `risk.py`, `notify.py`, `lock.py`,
`net.py`, `snapshot.py`, `digest.py`, `replay.py`, `scorecard.py`, `watchdog.py`.
They survive because `data.py` normalises klines into the same
`{t,o,h,l,c,v}` bar shape the pipeline already speaks — the same trick that
carried the Alpaca → Indodax port.

`strategy.py` is ported and extended: two new rungs in the cascade, and
`check_exit` takes the 15m bar for price while continuing to read `atr14` from
the 1H rung.

`saldo.py` ports with IDR formatting replaced by USDT.

## Data layer

Kline endpoint (`type: 1` symbols): `GET https://www.tokocrypto.site/api/v3/klines`
with `symbol`, `interval`, `limit` (max 1000), optional `startTime`/`endTime` in
milliseconds. Response is `{"code":0,"data":[[openTime,o,h,l,c,v,closeTime,...]]}`.

Bars per timeframe, sized for EMA55 + ADX14 warmup with margin:

| TF | Interval code | Bars fetched | Refresh |
| --- | --- | --- | --- |
| 15m | `15m` | 500 | every cycle |
| 30m | `30m` | 500 | every cycle |
| 1H | `1h` | 500 | once per hour |
| 4H | `4h` | 300 | once per hour |
| 1D | `1d` | 300 | once per hour |

**Rate-limit budget.** Tokocrypto limits by IP, not by key, and answers abuse
with a 418 ban escalating from 2 minutes to 3 days. Naive fetching would be
10 coins × 5 timeframes × 4 cycles = 200 kline calls/hour. The refresh column
above cuts that to 10×2×4 + 10×3 = **110 calls/hour**, plus roughly 10 account
and order calls — comfortably inside the published weight allowance, with the
`X-MBX-USED-WEIGHT-*` response header logged each cycle so drift is visible
before it becomes a ban.

Type-3 ("Nextme") symbols are served by a different kline host and carry no
ticker data. They are excluded from the watchlist entirely.

## Cost model

Tokocrypto's trade payload (`GET /open/v1/orders/trades`) reports `commission`
and `commissionAsset` **and** separate `taxAmount` / `taxRate` — Indonesian
withholding charged on top of the exchange fee.

All three are recorded into the ledger as **net from the first commit**. This is
a direct consequence of CryptoAutoBot's IMP-B01: that bot reported gross P&L as
net for months because no fee term existed anywhere, and the gap only surfaced
during broker reconciliation ($463, 0.206% per side, 22% of gross profit).

**Assumption to replace:** until the user reports their actual Tokocrypto fee
tier, the cost model seeds `TAKER_FEE_PCT = 0.001` and `TAX_PCT = 0.0021`, giving
a round trip near 0.41% and `MIN_ATR_PCT ≈ 0.49%`. These are placeholders with
plausible magnitudes, not measured values. The replay harness recalibrates
`OBSERVED_ROUND_TRIP_PCT` from real fills once 20 round trips exist, exactly as
CryptoIndodaxBot did when it discovered its configured 0.4% was really 0.63%.

## Cadence

```cron
2,17,32,47 * * * * cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.snapshot >> logs/snapshot.log 2>&1
6,21,36,51 * * * * cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.trader   >> logs/trader.log 2>&1
```

Offsets sit clear of CryptoAutoBot (`:05`/`:12`) and CryptoIndodaxBot
(`:07`/`:14`) so the three bots never contend for CPU or hit their exchanges in
the same second. The four-minute snapshot→trader gap matches the existing
design; `SNAPSHOT_MAX_AGE_MIN` drops from 70 to **20** so a missed snapshot
halts trading within one cycle instead of one hour.

Snapshots move from `data/snapshots/YYYY-MM-DD/HH.json` to
`data/snapshots/YYYY-MM-DD/HH-MM.json`.

The order lock (`lock.py`) carries over unchanged and matters more here: cycles
are four times as frequent, so overlap between a slow trader run and the next
one is four times as likely. The trader waits for the lock rather than skipping,
because a skipped cycle suspends exits and the circuit breaker.

### Clock-based knobs need no rescaling

`TIME_STOP_HOURS = 120`, `REENTRY_THROTTLE_HOURS = 24`, `POLICY_MAX_AGE_HOURS = 48`
and the circuit breaker's rolling 24h window are all expressed in wall-clock
units, not bar counts. They carry over correctly and must **not** be divided by
four.

## Config deltas from CryptoIndodaxBot

Changed:

```
QUOTE                = "USDT"          (was "IDR")
TIMEFRAMES           = {"15m","30m","1H","4H","1D"}   (was 1H/4H/1D)
SNAPSHOT_MAX_AGE_MIN = 20              (was 70)
MIN_ORDER_USDT       = 5.0             (was MIN_ORDER_IDR 10_000)
DUST_USDT            = 5.0             (was DUST_IDR 10_000)
TAKER_FEE_PCT        = 0.001           (assumption, see Cost model)
TAX_PCT              = 0.0021          (new — Indonesian withholding)
DRY_RUN_EQUITY_USDT  = 1_000.0         (was DRY_RUN_EQUITY_IDR 10_000_000)
```

Carried over unchanged: `MAX_POSITIONS=4`, `RISK_PCT=0.015`, `STOP_ATR_MULT=3.0`,
`TRAIL_ATR_MULT=4.0`, `RISK_OFF_TRAIL_ATR_MULT=3.0`, `TP_R=2.5`,
`PROFIT_LOCK_RUNGS=((1.5,1.0),)`, `CIRCUIT_BREAKER_PCT=0.04`, `ENTRY_ADX_MIN=20.0`,
`ENTRY_ADX_MIN_CAUTIOUS=25.0`, `ENTRY_RSI_MIN=45.0`, `ENTRY_RSI_MAX=70.0`,
`BLOWOFF_RSI=80.0`, `LATE_ENTRY_DAY_PCT=5.0`, `MAX_FEE_DRAG_R=0.28`,
EMA/RSI/ATR/ADX/VOL periods.

Watchlist: `BTC, ETH, SOL, XRP, DOGE, AVAX, LINK, DOT, LTC, UNI`. BTC is
mandatory — it drives the regime gate, and its absence disables entries rather
than silently defaulting to risk_on.

## Replay harness and the go-live gate

Unlike Indodax, Tokocrypto serves deep kline history on demand, so the backtest
does not have to wait for snapshots to accumulate. `replay.py` is built from
klines paged via `startTime`/`endTime` and drives the real `strategy` and `risk`
engines, as CryptoIndodaxBot's does.

`TRADING_ENABLED=false` until the replay clears, and the replay reports under
the standing stop doctrine:

- trades bucketed **WIN / SCRATCH / FAIL off `profit_R`**, never off the sign of P&L
- **stop rate reported beside true win rate**, both next to the headline number
- a verdict of **"no demonstrated edge"** rather than knob-tuning when it fails
- stops are never widened, removed, or weakened to flatter the metric

CryptoAutoBot's IMP-B03 is the precedent: its first honest backtest said no
demonstrated edge, underperforming buy-and-hold by 8.15pp, and no config
shipped. The same outcome is acceptable here.

## Known traps carried forward

1. **IPv6 breaks API-key IP whitelists on this VPS.** `getaddrinfo` prefers the
   v6 address, so the exchange sees `2a02:c207:…` rather than `185.202.236.11`.
   This cost real debugging time on Indodax (`[-2015] Unauthorized IP` on valid
   credentials). `net.force_ipv4()` is wired in from the first commit, not added
   after the first failure.
2. **A restart is not a deploy.** CryptoIndodaxBot's IMP-003 was reported live
   while the old process kept running the old code. Verify fixes by comparing
   `systemctl show -p ActiveEnterTimestamp` against file mtime, or for cron, by
   reading the log line the new code emits.
3. **Fees taken in-asset drift the held quantity below the fill.** Sell the
   broker's reported balance, not the ledger's number.
4. **No sandbox is assumed.** Tokocrypto operates `demo.tokocrypto.com`, but it
   is unverified for this account and is not part of the plan. `--dry-run` plus
   the replay harness are the safety net; live trading spends real funds.

## Out of scope

WebSocket streams (the 15-minute cadence does not need sub-second data, and REST
stays inside the rate limit); the Telegram command listener and daily Claude
digest routine (ported after the core bot is validated, mirroring how
CryptoIndodaxBot sequenced them); margin, futures and limit-order entries.

## Prerequisites on the user

These gate live trading, not development — the backtest runs entirely on public
endpoints.

1. Tokocrypto account with KYC.
2. API key with spot trading enabled, IP `185.202.236.11` whitelisted.
3. USDT balance (via `USDT_IDR`, the deepest IDR book on the exchange at
   Rp53bn/24h).
4. Actual maker/taker fee tier, to replace the seeded assumption above.
5. A Telegram bot token and chat ids.
