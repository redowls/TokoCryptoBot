"""The tuner's job is to REFUSE most candidates.

Every test here is about a way an optimiser talks itself into a change it has
not earned: a gain only on the slice it searched, a gain inside the noise
floor, a gain on three trades, a gain bought by worsening the stop rate, or
six changes a day so none is ever observed before the next.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from tokocrypto import config, tuner


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def result(net, trades=40, pf=1.2, stop=0.5):
    return {"net_pct": net, "trades": trades, "profit_factor": pf,
            "true_win_rate": 0.4, "stop_rate": stop, "buy_and_hold": 100.0}


@pytest.fixture
def scored(monkeypatch):
    """Drive search() with a scoring function instead of a real backtest."""
    def install(fn):
        monkeypatch.setattr(tuner, "evaluate", fn)
    return install


def test_split_holds_back_the_newest_frames_not_a_random_sample():
    frames = list(range(100))
    train, oos = tuner.split(frames, train_frac=0.65)
    assert train == list(range(65))
    assert oos == list(range(65, 100)), "the holdout must be the FUTURE"


def test_gain_only_on_train_is_rejected(scored):
    """The signature of overfitting: better where it searched, not after."""
    def ev(frames, values):
        on_train = len(frames) == 100
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(20.0 if on_train else -5.0)
        return result(0.0)
    scored(ev)

    best, evidence = search_adx(train_len=100, oos_len=50)
    assert best.get("ENTRY_ADX_MIN") != 30.0
    assert any("noise" in r["why"] or "margin" in r["why"]
               for r in evidence["rejected"])


def test_gain_on_both_slices_is_accepted(scored):
    def ev(frames, values):
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(12.0)
        return result(0.0)
    scored(ev)

    best, evidence = search_adx(train_len=100, oos_len=50)
    assert best["ENTRY_ADX_MIN"] == 30.0
    assert evidence["accepted"], "a genuine two-slice gain should be banked"


def test_tiny_oos_sample_is_refused_however_good_it_looks(scored):
    def ev(frames, values):
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(99.0, trades=3 if len(frames) == 50 else 40)
        return result(0.0)
    scored(ev)

    best, evidence = search_adx(train_len=100, oos_len=50)
    assert best.get("ENTRY_ADX_MIN") != 30.0
    assert any("OOS trades" in r["why"] for r in evidence["rejected"])


def test_improvement_inside_the_noise_floor_is_refused(scored):
    def ev(frames, values):
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(tuner.MARGIN_PCT / 2)
        return result(0.0)
    scored(ev)
    best, _ = search_adx(train_len=100, oos_len=50)
    assert best.get("ENTRY_ADX_MIN") != 30.0


def test_gain_bought_with_a_worse_stop_rate_is_refused(scored):
    """Exactly the trade the standing doctrine forbids."""
    def ev(frames, values):
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(12.0, stop=0.9)
        return result(0.0, stop=0.5)
    scored(ev)

    best, evidence = search_adx(train_len=100, oos_len=50)
    assert best.get("ENTRY_ADX_MIN") != 30.0
    assert any("stop rate" in r["why"] for r in evidence["rejected"])


def test_falling_profit_factor_is_refused(scored):
    def ev(frames, values):
        if values.get("ENTRY_ADX_MIN") == 30.0:
            return result(12.0, pf=0.6)
        return result(0.0, pf=1.2)
    scored(ev)
    best, evidence = search_adx(train_len=100, oos_len=50)
    assert best.get("ENTRY_ADX_MIN") != 30.0
    assert any("profit factor" in r["why"] for r in evidence["rejected"])


def test_search_halts_when_the_window_is_too_thin_to_judge(scored):
    scored(lambda frames, values: result(0.0, trades=4))
    best, evidence = search_adx(train_len=100, oos_len=50)
    assert "halt" in evidence
    assert best == {}


def search_adx(train_len, oos_len):
    return tuner.search(list(range(train_len)), list(range(oos_len)),
                        {}, knobs=["ENTRY_ADX_MIN"], log_fn=lambda *_: None)


# --- throttle and revert ----------------------------------------------------

def test_one_change_per_day():
    recent = {"history": [{"at": (NOW - timedelta(hours=3)).isoformat(),
                           "action": "apply"}]}
    old = {"history": [{"at": (NOW - timedelta(days=2)).isoformat(),
                        "action": "apply"}]}
    assert tuner.throttled(recent, now=NOW) is True
    assert tuner.throttled(old, now=NOW) is False
    assert tuner.throttled({"history": []}, now=NOW) is False


def test_a_revert_does_not_count_as_a_change_for_the_throttle():
    state = {"history": [{"at": (NOW - timedelta(hours=1)).isoformat(),
                          "action": "revert"}]}
    assert tuner.throttled(state, now=NOW) is False


def test_regression_is_detected_against_the_oos_that_justified_the_change():
    state = {"history": [{"at": NOW.isoformat(), "action": "apply",
                          "oos": {"net_pct": 5.0}}]}
    assert tuner.check_regression(result(4.9), state) is None
    decayed = tuner.check_regression(result(5.0 - 3 * tuner.MARGIN_PCT), state)
    assert decayed and "decayed" in decayed


def test_no_prior_application_means_nothing_to_regress_against():
    assert tuner.check_regression(result(-50.0), {"history": []}) is None


def test_apply_writes_the_overlay_and_its_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TUNING_PATH", tmp_path / "tuning.json")
    monkeypatch.setattr(tuner, "STATE_PATH", tmp_path / "state.json")
    state = {"history": []}

    tuner.apply({"ENTRY_ADX_MIN": 27.0},
                {"final": {"oos": result(6.0)}, "baseline": {"oos": result(1.0)}},
                state, now=NOW)

    written = json.loads((tmp_path / "tuning.json").read_text())
    assert written["values"] == {"ENTRY_ADX_MIN": 27.0}
    assert written["evidence"]["oos"]["net_pct"] == 6.0, "must record its own justification"
    assert json.loads((tmp_path / "state.json").read_text())["history"][-1]["action"] == "apply"


def test_revert_removes_the_overlay_and_records_why(tmp_path, monkeypatch):
    path = tmp_path / "tuning.json"
    path.write_text(json.dumps({"values": {"ENTRY_ADX_MIN": 27.0}}))
    monkeypatch.setattr(config, "TUNING_PATH", path)
    monkeypatch.setattr(tuner, "STATE_PATH", tmp_path / "state.json")

    tuner.revert({"history": []}, "OOS decayed", now=NOW)
    assert not path.exists(), "reverting must restore the defaults"
    entry = json.loads((tmp_path / "state.json").read_text())["history"][-1]
    assert entry["action"] == "revert" and entry["why"] == "OOS decayed"


def test_grid_stays_inside_each_knob_declared_range():
    for knob in config.TUNABLE:
        lo, hi = config.TUNABLE[knob]
        values = tuner._grid(knob)
        assert values, knob
        assert min(values) >= lo and max(values) <= hi, knob


def test_grid_cannot_propose_a_wider_stop_than_the_default():
    """The grid's upper bound is the declared range, but apply_tuning refuses
    anything above the default. Confirm the two agree so the search does not
    spend its budget on candidates that can never be written."""
    default = config._DEFAULTS["STOP_ATR_MULT"]
    for value in tuner._grid("STOP_ATR_MULT"):
        applied = config.apply_tuning(values={"STOP_ATR_MULT": value})
        assert config.STOP_ATR_MULT <= default
        if value > default:
            assert "STOP_ATR_MULT" not in applied
    config.apply_tuning()
