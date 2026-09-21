# TokoCryptoBot

15-minute crypto trading bot for **Tokocrypto**, quoted in **USDT**.

Ported from [CryptoIndodaxBot](https://github.com/redowls/CryptoIndodaxBot)
(Indodax / IDR), itself ported from
[CryptoAutoBot](https://github.com/redowls/CryptoAutoBot) (Alpaca / USD). The
risk model, ledger and policy overlay are unchanged. What is new is the exchange
adapter and a **timing layer**: 15m and 30m candles beneath the inherited
1D/4H/1H decision cascade, with the cycle running every 15 minutes instead of
hourly.

## ⚠️ Read this before enabling trading

**The backtest currently says this strategy has NO DEMONSTRATED EDGE.**

The first run over 10 days of live Tokocrypto data (2026-09-19, recorded in
`memory/backtest-2026-09-19-baseline.txt`):

```
VERDICT: NO DEMONSTRATED EDGE
  equity        $1,000.00 -> $901.44  (-9.86%)
  buy-and-hold  $946.40  (-4.75pp vs strategy)
  trades        11
  true win rate 9%   (WIN 1 / SCRATCH 0 / FAIL 10)
  stop rate     91%
  profit factor 0.08
```

`TRADING_ENABLED=false` until that changes. Ten days is a short window and the
sample is small, but the burden of proof runs the other way: the bot stays off
until evidence says otherwise, not until evidence says stop.

Tokocrypto has **no verified sandbox** for this account, so every order placed
with trading enabled is real money. Preview with `--dry-run`.

## The timeframe cascade

| Timeframe | Role |
| --- | --- |
| 1D | BTC regime gate (EMA stack + ADX) — inherited |
| 4H | veto: reject if the stack is DOWN — inherited |
| 1H | qualification: ADX / RSI / ATR floor / stack UP — inherited |
| **30m** | confirmation: reject if the stack is DOWN — new |
| **15m** | trigger and exit evaluation, every close — new |

The higher three rungs decide **whether** a coin is tradeable; the lower two
decide **when**. The timing rungs are evaluated *last* so the rejection log
keeps "eligible, not yet triggered" distinct from "not eligible" — at four
cycles an hour that log is the main window into the bot's behaviour.

### The load-bearing decision: 1R is anchored to the 1H ATR

Exits evaluate on 15m closes, but `1R` is always computed from the **1H** ATR.

Measured on BTC, 2026-09-19:

| | 15m | 1H |
| --- | --- | --- |
| ATR | 0.217% | 0.598% |
| 3×ATR stop | 0.65% | 1.79% |
| fee drag (0.41% round trip) | **63% of 1R** | **23% of 1R** |

`MAX_FEE_DRAG_R` is 0.28. Anchoring R to the 15m bar would blow through that
ceiling by more than double and kill the edge arithmetically before the strategy
ran a single cycle. `strategy.check_exit(position, signal_tf, anchor_tf, ...)`
takes the two blocks separately to make that mistake hard to make by accident.

**Any change that re-anchors R to a faster timeframe must re-derive the
fee-drag ceiling first, not adjust it afterwards.**

## Status: shadow mode

Cron is installed and the bot runs its full 15-minute cycle, but
`TRADING_ENABLED=false`: it decides, logs what it would have done, and places
no orders. That is deliberate. The gate has not passed, and the point of running
is to grow a live sample alongside the backtest before anyone concludes
anything.

| Item | Status |
| --- | --- |
| Cron installed | ✅ shadow mode — `scripts/install-cron.sh` |
| 4-hourly review routine | ✅ `tokocrypto-review`, delivers to Telegram |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | ✅ set and verified |
| `TOKOCRYPTO_API_KEY` / `_SECRET` | ❌ not set — trading impossible until they are |
| IP whitelist (`185.202.236.11`) | ⏳ pending key creation |
| Account funded (USDT) | ❌ empty |
| Real fee tier confirmed | ❌ **seeded assumption** — see Cost model |
| Backtest passes the go-live gate | ❌ **NO DEMONSTRATED EDGE** |

Gate history. **Runs 1 and 2 are void as evidence about the strategy** — both
predate `1a9d1fc`, which fixed `--days N` silently capping at the API's 1000-bar
limit. They measured ~10 days and a dozen trades, not the window asked for. Run
3 is the first valid measurement and supersedes them:

| Run | Window | Net | vs buy-and-hold | Trades | True win | Stop rate | PF | Payoff |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 baseline | 940 frames | −9.86% | −4.75pp | 11 | 9% | 91% | 0.08 | 0.82 |
| 2026-09-19 re-run | 940 frames | −11.51% | −6.46pp | 12 | 8% | 92% | 0.05 | — |
| **2026-09-19 paged** | **8,867 frames** | **−9.51%** | **−29.7pp** | **90** | **37%** | **58%** | **0.78** | **1.11** |

The earlier reading — "a 92% stop rate means the strategy is almost never right
about direction" — **was an artifact of the 11-trade sample and is retracted.**
On 90 trades the true win rate is 37%, the stop rate 58% and the payoff 1.11.
Direction is not the problem.

Costs are. Run 3 decomposes as:

```
gross win   $328.06
gross loss  $423.15
net         −$95.09
fees        $129.43     <- modelled, seeded at 0.31%/side
```

Net of fees the strategy loses $95. **Gross of fees it makes +$34.** The entire
loss, and more, is transaction cost: 90 round trips at a 15-minute cadence
against an edge too thin to pay for them. That is a different failure from a
broken signal and it has different remedies — fewer and higher-conviction
entries, maker rather than taker fills, or a `MIN_ATR_PCT` floor derived from
the *real* fee tier rather than the seeded one.

The buy-and-hold comparison is also worse than it first looks: the benchmark
returned **+28.7%** over the same window while the bot returned −9.5%. The
market rallied and the strategy lost money into it — the same shape as
CryptoAutoBot's IMP-B03 finding.

Confirming the real fee tier is now the highest-value missing input; every
number above moves with it.

Development and backtesting need none of these — every public endpoint works
unauthenticated.

## Layout

```
tokocrypto/
  config.py      constants, env loading, the two symbol spellings
  net.py         force_ipv4()
  indicators.py  EMA / RSI / ATR / ADX / volume       (ported verbatim)
  data.py        kline fetch, normalise, drop the in-progress bar
  symbols.py     metadata cache, LOT_SIZE / NOTIONAL filters
  snapshot.py    five-timeframe capture, refresh-gated
  strategy.py    the cascade, entry and exit rules
  risk.py        sizing, circuit breaker
  ledger.py      positions, net-of-fee P&L, re-entry throttle
  policy.py      daily policy overlay                  (ported verbatim)
  broker.py      Tokocrypto MBX signed REST client
  notify.py      Telegram fan-out
  lock.py        advisory order lock
  trader.py      the 15-minute cycle
  replay.py      kline-driven backtest over the real engines
  scorecard.py   pre-registered live decision criterion
  review.py      4-hourly self-review report
```

## Running

```bash
python -m tokocrypto.snapshot           # capture one 15-minute snapshot
python -m tokocrypto.trader --dry-run   # decide, place nothing
python -m tokocrypto.replay --days 10   # backtest — the go-live gate
python -m tokocrypto.replay --days 10 --fee-pct 0.005   # cost sensitivity
python -m tokocrypto.scorecard          # live decision scorecard
python -m tokocrypto.review             # the 4-hourly report
python -m pytest tests/ -q
```

Installed cron:

```cron
2,17,32,47 * * * * cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.snapshot >> logs/snapshot.log 2>&1
6,21,36,51 * * * * cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.trader   >> logs/trader.log 2>&1
10 */4 * * * /root/claude-routines/run-routine.sh tokocrypto-review
```

Offsets sit clear of CryptoAutoBot (`:05`/`:12`) and CryptoIndodaxBot
(`:07`/`:14`) so the three bots never contend.

Install or refresh them with `bash scripts/install-cron.sh` — never by hand.
`crontab -` replaces the whole table, which on this VPS carries every other
bot, so the script backs up first, touches only its own marked block, and
restores the backup if the active line count moves by anything other than its
own three lines.

The review routine lives at `/root/claude-routines/tokocrypto-review.{md,conf}`
and runs read-only: the `.conf` does not widen `run-routine.sh`'s default
`ALLOWED_TOOLS`, so with no Write or Edit tool the routine is structurally
unable to widen a stop, loosen a filter, or flip `TRADING_ENABLED`. Against a
repeatedly-failing backtest, that is the temptation worth removing with tooling
rather than with prose.

## Cost model

Tokocrypto reports `commission` **and** a separate `taxAmount` (Indonesian
withholding) on every fill. Both are money leaving the account and both are
summed into the ledger's cost. Modelling only the commission is how CryptoAutoBot
reported gross P&L as net for months — a $463 gap, 22% of gross profit, invisible
until someone reconciled against the broker.

The configured `TAKER_FEE_PCT = 0.001` and `TAX_PCT = 0.0021` are **seeded
assumptions with plausible magnitudes, not measurements**. Replace them with the
real tier. CryptoIndodaxBot's configured 0.4% turned out to be 0.63% on real
fills, and that gap is the difference between a strategy that pays for itself
and one that does not.

## What changed from CryptoIndodaxBot

| Area | Indodax | Tokocrypto |
| --- | --- | --- |
| Quote currency | IDR | **USDT** |
| Why | — | only 40 IDR pairs; LINK, DOT and UNI have none |
| Bars | `tradingview/history_v2`, from/to window | `api/v3/klines`, `limit`-based, arrays not objects |
| Bar payload | `{Time,Open,…}`, unix **seconds** | `[openTime,o,h,l,c,v,closeTime,…]`, **milliseconds** |
| In-progress bar | not an issue hourly | **must be dropped** — flickers every indicator at 15m |
| Auth | HMAC-SHA256, `X-APIKEY` + `Sign` | HMAC-SHA256, `X-MBX-APIKEY` + `&signature=` |
| Symbol spelling | `BTCIDR` everywhere | **`BTC_USDT`** for `/open/v1`, **`BTCUSDT`** for klines |
| Order side/type | strings | ints — `side` 0=BUY/1=SELL, `type` 2=MARKET |
| Balances | one call, all assets | **one call per asset** — no bulk endpoint |
| Minimums | Rp10.000 | `$5` NOTIONAL (`$1` DOGE) + `LOT_SIZE` step |
| Fees | commission only | commission **+ `taxAmount`** |
| Backtest history | only accumulated snapshots | **klines on demand** — gate runs on day one |
| Cycle | hourly | **every 15 minutes** |

## Traps worth remembering

1. **IPv6 silently breaks API-key IP whitelists on this VPS.** `getaddrinfo`
   prefers the v6 address, so the exchange sees `2a02:c207:…` rather than
   `185.202.236.11` and rejects valid credentials. `net.force_ipv4()` is wired
   in from the first commit rather than after the first failure.
2. **The last kline is still forming.** Letting a partial bar into the
   indicators makes every EMA and ADX flicker within the bar, and at a
   15-minute cadence that flicker *is* the signal. `data.fetch_klines` drops it
   by default.
3. **1R comes from the 1H ATR.** See above. This is not a style preference.
4. **A restart is not a deploy.** CryptoIndodaxBot once reported a fix live
   while the old process kept running the old code. Verify by comparing
   `systemctl show -p ActiveEnterTimestamp` against file mtime, or for cron, by
   reading the log line only the new code emits.
5. **Fees are taken in-asset**, so the held quantity drifts below the fill. Sell
   the broker's reported free balance floored to `stepSize`, never the ledger's
   number.
6. **`round_qty` floors, never rounds up.** Rounding up funds an order the
   balance cannot cover. The quotient is rounded to 9 places before flooring so
   binary float does not silently shrink an order by one whole step.

## Doctrine

Any stop-triggered exit is a **FAILURE** whatever the P&L sign — a +0.0%
break-even stop is not a win. Trades bucket WIN / SCRATCH / FAIL off
`r_multiple`, never off the sign of P&L, and stop rate is always reported beside
true win rate. Counting `pnl > 0` as the win rate overstated USTradeBot's by 90%.

Stops are never widened, loosened or removed to improve a metric. When the
backtest says no demonstrated edge, that is the finding — repeated failure
escalates to retire-or-rebuild, not to another parameter search.
