import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "snapshots"
MEMORY_DIR = ROOT / "memory"
LOG_DIR = ROOT / "logs"
TRADES_DIR = ROOT / "data" / "trades"
POLICY_PATH = MEMORY_DIR / "policy.json"
SYMBOLS_CACHE = ROOT / "data" / "symbols.json"


def _load_dotenv(path=ROOT / ".env"):
    # cron runs without a login shell; pick up keys from .env ourselves
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# --- universe -------------------------------------------------------------
# All ten are live *_USDT pairs of type 1 (Binance-matched), verified against
# GET /open/v1/common/symbols on 2026-09-19: $5 minNotional, $1 on DOGE.
#
# This list is the IDR watchlist from CryptoIndodaxBot, which survives intact
# only because we quote in USDT. Tokocrypto lists just 40 IDR pairs and three
# of these coins — LINK, DOT and UNI — have no IDR market at all.
#
# BTC must stay on this list whatever else changes: strategy.regime() reads
# BTC's 1D timeframe to set the risk_on/neutral/risk_off gate for every other
# coin, and falls back to "neutral" if BTC is absent — which silently raises
# the ADX entry bar across the board.
WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "DOT", "LTC", "UNI"]

QUOTE = "USDT"


def pair(sym: str) -> str:
    """Spelling used by /open/v1/* endpoints: BTC -> BTC_USDT."""
    return f"{sym.upper()}_{QUOTE}"


def chart(sym: str) -> str:
    """Spelling used by the kline and depth hosts: BTC -> BTCUSDT.

    Tokocrypto serves the same symbol under two spellings depending on the
    endpoint family. Keeping both constructions here means no caller has to
    remember which host wants which.
    """
    return f"{sym.upper()}{QUOTE}"


# --- endpoints ------------------------------------------------------------
# Signed calls and symbol metadata live on tokocrypto.com; klines and depth for
# type-1 symbols live on tokocrypto.site. Both are documented as current.
OPEN_BASE_URL = "https://www.tokocrypto.com"
KLINE_BASE_URL = "https://www.tokocrypto.site"
SYMBOLS_PATH = "/open/v1/common/symbols"
KLINES_PATH = "/api/v3/klines"

USER_AGENT = "TokoCryptoBot/1.0 (+https://github.com/redowls/TokoCryptoBot)"

# --- the timeframe cascade ------------------------------------------------
# 1D regime -> 4H veto -> 1H qualify -> 30m confirm -> 15m trigger/exit.
#
# The higher three rungs are inherited unchanged from CryptoIndodaxBot and
# decide WHETHER a coin is tradeable. The lower two are new and decide WHEN.
# Sliding the whole ladder down (15m signal, 30m veto, 1H regime) was rejected:
# it discards the calibration the 1D/4H/1H cascade earned and multiplies entry
# count, which is what drives fee drag.
TIMEFRAMES = {"15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h", "1D": "1d"}

REGIME_TF = "1D"
VETO_TF = "4H"
QUALIFY_TF = "1H"
CONFIRM_TF = "30m"
SIGNAL_TF = "15m"

# LOAD-BEARING. 1R is computed from the 1H ATR even though exits evaluate on
# 15m closes. A 15m ATR is roughly a third of the 1H ATR, so anchoring R there
# would cut stop distance to a third — and since fee drag is a fraction of 1R,
# it would push drag from ~31% of 1R to near 90%, killing the edge
# arithmetically before the strategy ran. Any change here must re-derive the
# fee-drag ceiling FIRST. See docs/superpowers/specs/.
ATR_ANCHOR_TF = "1H"

# Bars per request, sized so EMA55 and ADX14 are always warm. The kline
# endpoint caps at 1000.
BAR_LIMIT = {"15m": 500, "30m": 500, "1H": 500, "4H": 300, "1D": 300}

# Tokocrypto rate-limits by IP and answers abuse with a 418 ban that escalates
# from 2 minutes to 3 days. Refetching 4H and 1D every 15 minutes would be 3x
# the necessary traffic for data that cannot have changed, so the higher
# timeframes refresh hourly and snapshot.py carries the rest forward.
REFRESH_EVERY_MIN = {"15m": 15, "30m": 15, "1H": 60, "4H": 60, "1D": 60}

EMA_PERIODS = (8, 20, 55)
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOL_PERIOD = 20

# --- private API ----------------------------------------------------------
RECV_WINDOW_MS = 5000

TOKO_KEY = os.getenv("TOKOCRYPTO_API_KEY")
TOKO_SECRET = os.getenv("TOKOCRYPTO_API_SECRET")

# This host has both IPv4 and IPv6, and tokocrypto.com publishes both A and
# AAAA records — so requests leave over IPv6 by default and the key's IPv4
# whitelist never matches. Pin to IPv4 so the source address is deterministic
# and whitelistable. This cost real debugging time on Indodax; it is wired in
# here from the first commit rather than after the first failure. See net.py.
FORCE_IPV4 = os.getenv("FORCE_IPV4", "true").lower() == "true"

# --- trading --------------------------------------------------------------
# Tokocrypto has no verified sandbox for this account, so every order placed
# with TRADING_ENABLED=true is REAL money. Preview with:
#   python -m tokocrypto.trader --dry-run
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"

MAX_POSITIONS = 4           # hard cap; the daily policy may lower it, never raise it
RISK_PCT = 0.015            # equity fraction risked per trade
STOP_ATR_MULT = 3.0         # initial stop distance = 1R (measured on the 1H ATR)
TRAIL_ATR_MULT = 4.0        # trail distance once >= +1R
RISK_OFF_TRAIL_ATR_MULT = 3.0  # tighter trail while BTC regime is risk_off
TP_R = 2.5                  # hard take-profit in R multiples
PROFIT_LOCK_RUNGS = ((1.5, 1.0),)

# Clock-based, NOT bar-based. The cycle is four times faster than
# CryptoIndodaxBot's but these are wall-clock durations, so they carry over
# unchanged and must not be divided by four.
TIME_STOP_HOURS = 120
CIRCUIT_BREAKER_PCT = 0.04  # rolling 24h realized loss halts new entries
REENTRY_THROTTLE_HOURS = 24
POLICY_MAX_AGE_HOURS = 48   # stale policy.json is ignored

# 20 minutes, not 70. At a 15-minute cadence a stale snapshot is far more
# dangerous than it was hourly: this budget halts trading within one cycle of a
# missed capture instead of letting the bot act on hour-old indicators.
SNAPSHOT_MAX_AGE_MIN = 20

# Most USDT pairs enforce a $5 minNotional ($1 on DOGE); symbols.py reads the
# real per-symbol value and falls back to this. Coin dust below this notional is
# ignored when deriving positions from spot balances.
MIN_ORDER_USDT = 5.0
DUST_USDT = 5.0

# Equity used by --dry-run when no API credentials are configured.
DRY_RUN_EQUITY_USDT = 1_000.0

# --- entry filters (inherited, distilled from CryptoIndodaxBot insights) ---
ENTRY_ADX_MIN = 20.0
ENTRY_ADX_MIN_CAUTIOUS = 25.0  # when regime is neutral/risk_off
ENTRY_RSI_MIN = 45.0
ENTRY_RSI_MAX = 70.0
BLOWOFF_RSI = 80.0
LATE_ENTRY_DAY_PCT = 5.0    # skip coins already up more than this on the day

# --- cost model -----------------------------------------------------------
# ASSUMPTION, not a measurement. Tokocrypto reports `commission` and a separate
# `taxAmount` (Indonesian withholding) on every fill; both are money leaving the
# account. These seeds have plausible magnitudes and are replaced by the
# measured round trip once ~20 real fills exist — CryptoIndodaxBot's configured
# 0.4% turned out to be 0.63% in practice.
TAKER_FEE_PCT = 0.001    # 0.10% per side
TAX_PCT = 0.0021         # charged on top of the commission
OBSERVED_ROUND_TRIP_PCT = (TAKER_FEE_PCT * 2 + TAX_PCT) * 100   # = 0.41%

# --- volatility floor: refuse trades the fees eat -------------------------
# The veto is economic, not a magic number: cap the round trip at
# MAX_FEE_DRAG_R of 1R and solve for the ATR that implies. Because 1R is
# measured on the 1H ATR, this floor is too — a lively 15m bar cannot talk the
# bot into a coin whose hourly range cannot pay its own fees.
MAX_FEE_DRAG_R = 0.28            # fees may not exceed 28% of 1R
MIN_ATR_PCT = OBSERVED_ROUND_TRIP_PCT / MAX_FEE_DRAG_R / STOP_ATR_MULT

# --- telegram -------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# One or more chat ids, comma-separated — every alert goes to all of them.
# A single id keeps working unchanged.
TELEGRAM_CHAT_IDS = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",")
                     if c.strip()]

# Back-compat alias: the first configured chat.
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_IDS[0] if TELEGRAM_CHAT_IDS else None


def fmt_usdt(amount) -> str:
    """Format a USDT amount for logs and Telegram."""
    if amount is None:
        return "-"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def fmt_price(value) -> str:
    """A price that stays readable below $1.

    fmt_usdt rounds to cents, which is right for balances and useless for
    prices: DOGE trades around $0.16 and the sub-cent digits are the
    information. Anything at or above $1 keeps the familiar rounded form.
    """
    if value is None:
        return "-"
    v = float(value)
    if abs(v) >= 1:
        return fmt_usdt(v)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):.8f}".rstrip("0").rstrip(".")
