"""Claude daily policy overlay (memory/policy.json) with safety clamps.

The overlay can only make the engine more conservative: block symbols, lower
the position cap, or worsen the regime. A missing, invalid, or stale file
degrades to pure deterministic mode.
"""
import json
from datetime import datetime, timedelta, timezone

from . import config

DEFAULT = {"regime_hint": "auto", "blocked_symbols": [], "max_positions": config.MAX_POSITIONS}


def load(path=None, now=None):
    """Return a clamped policy dict; DEFAULT on any problem."""
    path = path or config.POLICY_PATH
    now = now or datetime.now(timezone.utc)
    try:
        raw = json.loads(path.read_text())
        written = datetime.strptime(raw["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(DEFAULT)
    if now - written > timedelta(hours=config.POLICY_MAX_AGE_HOURS):
        return dict(DEFAULT)
    hint = raw.get("regime_hint")
    blocked = raw.get("blocked_symbols")
    max_pos = raw.get("max_positions")
    return {
        "regime_hint": hint if hint in ("auto", "risk_on", "neutral", "risk_off") else "auto",
        "blocked_symbols": [s for s in blocked if isinstance(s, str)] if isinstance(blocked, list) else [],
        "max_positions": min(max_pos, config.MAX_POSITIONS)
        if isinstance(max_pos, int) and max_pos > 0 else config.MAX_POSITIONS,
    }
