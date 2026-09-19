"""The overlay is how an automated tuner changes the strategy.

It exists instead of letting anything edit config.py because a JSON file can be
diffed, reverted and audited, and because the knobs absent from TUNABLE are then
structurally unreachable rather than merely discouraged. Risk sizing and the
cost model are deliberately not tunable: an optimiser allowed to size up or make
fees look cheaper will always "improve".
"""
import json
import importlib

import pytest

from tokocrypto import config as config_module


def reload_with(tmp_path, payload):
    """Reload config with a tuning.json containing `payload`."""
    path = tmp_path / "tuning.json"
    if payload is not None:
        path.write_text(json.dumps(payload))
    cfg = importlib.reload(config_module)
    cfg.TUNING_PATH = path
    cfg.apply_tuning()
    return cfg


@pytest.fixture(autouse=True)
def _restore():
    yield
    importlib.reload(config_module)


def test_overlay_moves_a_tunable_knob(tmp_path):
    cfg = reload_with(tmp_path, {"values": {"ENTRY_ADX_MIN": 27.5}})
    assert cfg.ENTRY_ADX_MIN == 27.5


def test_risk_and_cost_knobs_are_not_reachable(tmp_path):
    """The optimiser's easiest wins are sizing up and pretending fees are
    smaller. Neither is in TUNABLE, so neither can be written."""
    for knob in ("RISK_PCT", "MAX_FEE_DRAG_R", "TAKER_FEE_PCT",
                 "TAX_PCT", "MAX_POSITIONS"):
        assert knob not in cfg_tunable(), f"{knob} must not be tunable"

    baseline = importlib.reload(config_module)
    risk_before = baseline.RISK_PCT
    cfg = reload_with(tmp_path, {"values": {"RISK_PCT": 0.99}})
    assert cfg.RISK_PCT == risk_before


def cfg_tunable():
    return importlib.reload(config_module).TUNABLE


def test_stops_can_tighten_but_never_widen(tmp_path):
    """The standing anti-gaming rule: widening a stop flatters every metric."""
    default = importlib.reload(config_module).STOP_ATR_MULT

    tighter = reload_with(tmp_path, {"values": {"STOP_ATR_MULT": default - 1.0}})
    assert tighter.STOP_ATR_MULT == default - 1.0

    wider = reload_with(tmp_path, {"values": {"STOP_ATR_MULT": default + 1.0}})
    assert wider.STOP_ATR_MULT == default, "a wider stop must be refused"


def test_min_atr_floor_is_rederived_when_the_stop_moves(tmp_path):
    """MIN_ATR_PCT is solved from the stop distance. A tuner that tightens the
    stop without re-deriving the floor silently lets fees eat a bigger share of
    1R -- the exact accounting the floor exists to prevent."""
    cfg = reload_with(tmp_path, {"values": {"STOP_ATR_MULT": 2.0}})
    expected = cfg.OBSERVED_ROUND_TRIP_PCT / cfg.MAX_FEE_DRAG_R / 2.0
    assert cfg.MIN_ATR_PCT == pytest.approx(expected)


def test_out_of_range_values_are_refused_not_clamped(tmp_path):
    """Clamping would silently accept a nonsense proposal as a boundary value
    and report success. Refusing keeps the default and is visible."""
    default = importlib.reload(config_module).ENTRY_RSI_MAX
    cfg = reload_with(tmp_path, {"values": {"ENTRY_RSI_MAX": 300.0}})
    assert cfg.ENTRY_RSI_MAX == default


def test_unknown_knob_is_ignored(tmp_path):
    cfg = reload_with(tmp_path, {"values": {"NOT_A_KNOB": 1}})
    assert not hasattr(cfg, "NOT_A_KNOB")


def test_rsi_band_cannot_be_inverted(tmp_path):
    """min above max admits nothing; the bot would silently stop trading."""
    default_min = importlib.reload(config_module).ENTRY_RSI_MIN
    cfg = reload_with(tmp_path, {"values": {"ENTRY_RSI_MIN": 75.0,
                                            "ENTRY_RSI_MAX": 65.0}})
    assert cfg.ENTRY_RSI_MIN == default_min


def test_absent_or_corrupt_overlay_leaves_defaults(tmp_path):
    defaults = importlib.reload(config_module)
    adx, stop = defaults.ENTRY_ADX_MIN, defaults.STOP_ATR_MULT

    cfg = reload_with(tmp_path, None)                 # no file
    assert (cfg.ENTRY_ADX_MIN, cfg.STOP_ATR_MULT) == (adx, stop)

    bad = tmp_path / "tuning.json"
    bad.write_text("{not json")
    cfg = importlib.reload(config_module)
    cfg.TUNING_PATH = bad
    cfg.apply_tuning()
    assert (cfg.ENTRY_ADX_MIN, cfg.STOP_ATR_MULT) == (adx, stop)
