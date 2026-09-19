#!/usr/bin/env bash
# Install (or refresh) TokoCryptoBot's cron lines.
#
# Idempotent: the block is delimited by MARK/ENDMARK, so re-running replaces
# it rather than appending a second copy.
#
# The crontab on this VPS carries every other bot's schedule too. `crontab -`
# replaces the WHOLE table, so the failure mode is not "my lines are wrong",
# it is "I silently deleted CryptoIndodaxBot". Hence: back up first, compare
# the line count after, and restore automatically if anything but this block
# changed.
set -euo pipefail

ROOT=/root/TokoCryptoBot
MARK="# --- TokoCryptoBot ---"
ENDMARK="# --- end TokoCryptoBot ---"
BACKUP="/root/claude-routines/backups/crontab.$(date +%Y%m%d-%H%M%S).pre-tokocrypto"

mkdir -p "$(dirname "$BACKUP")" "$ROOT/logs"

# Deploy the routine prompt and conf. /root/claude-routines is not a git repo,
# so anything written only there is one `rm` from gone and is backed up nowhere.
# The versioned copies in routines/ are the source of truth; this copies them
# into place. Edit them HERE and re-run, never in /root/claude-routines — a
# direct edit there is silently overwritten by the next run of this script.
for f in tokocrypto-review.md tokocrypto-review.conf; do
  if ! cmp -s "$ROOT/routines/$f" "/root/claude-routines/$f"; then
    cp "$ROOT/routines/$f" "/root/claude-routines/$f"
    echo "deployed routines/$f -> /root/claude-routines/$f"
  fi
done
crontab -l > "$BACKUP" 2>/dev/null || : > "$BACKUP"
echo "backed up current crontab -> $BACKUP"

before=$(grep -cvE '^\s*(#|$)' "$BACKUP" || true)
existing=$(sed -n "/^${MARK}$/,/^${ENDMARK}$/p" "$BACKUP" | grep -cvE '^\s*(#|$)' || true)

# Offsets are chosen to clear the siblings: CryptoAutoBot runs at :05/:12 and
# CryptoIndodaxBot at :07/:14, so three bots never hit the exchange at once.
# The trader trails its snapshot by 4 minutes, which is enough for five
# timeframes of klines to land but well inside the 15-minute frame.
{
  sed "/^${MARK}$/,/^${ENDMARK}$/d" "$BACKUP"
  cat <<CRON
${MARK}
# SHADOW MODE: TRADING_ENABLED=false in .env AND --dry-run on the trader.
#
# --dry-run is not belt-and-braces, it is load-bearing. Without it the trader
# sees TRADING_ENABLED=false, logs one line and exits before deciding anything
# -- the shadow record would be an empty log that looks like a quiet market.
# With it, the full cycle runs and logs every entry, exit and rejection while
# placing no orders and saving no ledger state.
#
# Removing --dry-run does NOT enable trading (TRADING_ENABLED still gates it);
# it silently stops the bot from thinking. Both must change to go live, and
# the go-live gate has not passed.
2,17,32,47 * * * * cd ${ROOT} && .venv/bin/python -m tokocrypto.snapshot >> ${ROOT}/logs/snapshot.log 2>&1
6,21,36,51 * * * * cd ${ROOT} && .venv/bin/python -m tokocrypto.trader --dry-run >> ${ROOT}/logs/trader.log 2>&1
10 */4 * * * /root/claude-routines/run-routine.sh tokocrypto-review >> /root/claude-routines/logs/cron.log 2>&1
${ENDMARK}
CRON
} | crontab -

after=$(crontab -l | grep -cvE '^\s*(#|$)' || true)
expected=$((before - existing + 3))

if [[ "$after" -ne "$expected" ]]; then
  echo "ABORT: expected ${expected} active lines, found ${after}. Restoring." >&2
  crontab "$BACKUP"
  exit 1
fi

echo "installed: ${after} active cron lines (was ${before})"
crontab -l | sed -n "/^${MARK}$/,/^${ENDMARK}$/p"
