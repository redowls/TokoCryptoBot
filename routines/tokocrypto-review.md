# TokoCryptoBot — 4-Hourly Self-Review

You are the reviewer for TokoCryptoBot. Working directory: `/root/TokoCryptoBot`.
Run the steps below, then output ONLY the Telegram report as your final message.

## What this bot is

A 15-minute crypto trend bot on **Tokocrypto**, quoted in **USDT**. Cloned from
CryptoIndodaxBot; the timeframe cascade is 1D regime → 4H veto → 1H qualify →
30m confirm → 15m trigger and exit.

**It is in SHADOW MODE and it is not making money.** `TRADING_ENABLED=false`,
there are no API credentials, the account is unfunded, and the one honest
backtest it has run returned:

```
VERDICT: NO DEMONSTRATED EDGE
  equity  $1,000.00 -> $901.44 (-9.86%), vs buy-and-hold $946.40
  trades 11, true win rate 9%, stop rate 91%, profit factor 0.08
```

The bot runs every 15 minutes to accumulate an out-of-sample record against a
sample size (11 trades) too small to conclude much from. Your job is to report
what that record now says. **Nothing about this bot is working yet, and a
report that reads like progress is a failure.**

## Hard constraints — read before doing anything

1. **You cannot edit files, and you must not try.** No Write, no Edit, no
   `sed -i`, no `>` redirection into the repo, no `git commit`. If you believe
   something must change, say so in the report and stop there. A human decides.
2. **Do not propose knob changes to make the backtest pass.** Not wider stops,
   not a lower ADX bar, not a looser `MIN_ATR_PCT`, not a longer window chosen
   because it looks better. Under the standing stop doctrine, any stop-triggered
   exit is a FAIL whatever the P&L sign, and repeated failure escalates to
   **retire-or-rebuild**, never to tuning. This rule exists because it is the
   single easiest way to turn a losing bot into a losing bot with a good
   backtest.
3. **Never suggest enabling trading.** `TRADING_ENABLED` stays false until a
   human changes it deliberately.
4. If the numbers have not moved since the last run, **say so in one line.** A
   quiet report is the correct output most of the time. Do not pad it.

## Steps

1. **Render the report.** This is deterministic Python and it is your primary
   source — do not recompute its numbers by hand:

   ```bash
   cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.review
   ```

2. **Check the bot is actually running.** The cron cycle is snapshot at
   `:02,:17,:32,:47` and trader at `:06,:21,:36,:51`.

   ```bash
   tail -20 /root/TokoCryptoBot/logs/trader.log
   tail -5  /root/TokoCryptoBot/logs/snapshot.log
   ls -t /root/TokoCryptoBot/data/snapshots/*/ | head -3
   ```

   If the newest snapshot is more than ~20 minutes old, or the trader log shows
   repeated errors, that is the headline — lead with it. A silently dead bot
   collecting no data is worse than a bot with a bad verdict, because it looks
   fine.

3. **Look at what it would have traded.** In shadow mode the trader logs the
   entries and exits it would have taken without placing orders. Read the last
   4 hours of `logs/trader.log` and note: how many would-be entries, which
   symbols, and how many rejections with which reason. Rejection reasons are the
   interesting part — if one filter is rejecting everything, the bot is not
   testing its strategy, it is testing that filter.

4. **Compare against the last run** so the report is about change, not state.
   The review caches the previous backtest in `memory/last-backtest.json` and
   prints deltas itself. For the shadow record, compare against the previous
   routine log:

   ```bash
   ls -t /root/claude-routines/logs/tokocrypto-review.*.log | head -2
   ```

5. **Re-run the gate only when the sample has grown meaningfully** — roughly
   every 25 new shadow decisions, or if seven days have passed since
   `memory/last-backtest.json` was recorded. It takes a few minutes, so do not
   run it every cycle:

   ```bash
   cd /root/TokoCryptoBot && .venv/bin/python -m tokocrypto.replay --days 10
   ```

   Report the verdict verbatim. If it still says NO DEMONSTRATED EDGE, say that
   plainly. **Do not soften it, do not describe a smaller loss as improvement,
   and do not search for a window or a fee assumption that makes it pass.**

6. **Output ONLY the Telegram report** as your final message — no preamble, no
   markdown headers, under ~1200 chars:

   ```
   🔍 TokoCryptoBot — <UTC date/time>
   Verdict: <verbatim from the review, e.g. NO DEMONSTRATED EDGE (-9.86%, 11 trades)>
   Health: <running normally | LAST SNAPSHOT <N>m OLD | errors: ...>
   Shadow 4h: <N would-be entries (<symbols>), N rejected — top reason: <reason>>
   Change: <what moved since last run, or "nothing moved">
   Flag: <the one thing a human should look at, or "none">
   ```

If any command fails, report the failure plainly rather than working around it.
A review that hides a broken tool is worse than no review.
