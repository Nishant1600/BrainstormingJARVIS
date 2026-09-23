"""A stored percentage must not be run through the fraction heuristic again.

`record()` normalises the CLI's fraction (0.01) into a percentage (1.0) and
stores that. `_window_view` then applied the same conversion to the stored
value, so any window at 1% or less satisfied `value <= 1.0` and was multiplied
back up into a full 100% gauge. Observed live: a 7-day window truthfully at 1%
was reported as 100%, while the 5-hour window at 9% read correctly — which is
why it went unnoticed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import usage_store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    import data_paths
    monkeypatch.setattr(data_paths, "usage_path", lambda: tmp_path / "usage.json")
    yield


def _event(five: float, seven: float) -> dict:
    return {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": five, "status": "allowed"},
            "seven_day": {"utilization": seven, "status": "allowed"},
        },
    }


def _util(snapshot: dict, key: str):
    return next(w["utilization"] for w in snapshot["windows"] if w["key"] == key)


def test_one_percent_survives_the_round_trip():
    """The regression: 0.01 in, 1.0 out — not 100.0."""
    usage_store.record(_event(0.09, 0.01))
    snap = usage_store.snapshot()
    assert _util(snap, "seven_day") == 1.0
    assert _util(snap, "five_hour") == 9.0


def test_a_window_at_exactly_one_percent_is_not_a_full_gauge():
    usage_store.record(_event(0.5, 0.010))
    assert _util(usage_store.snapshot(), "seven_day") != 100.0


def test_ordinary_values_are_unaffected():
    usage_store.record(_event(0.62, 0.84))
    snap = usage_store.snapshot()
    assert _util(snap, "five_hour") == 62.0
    assert _util(snap, "seven_day") == 84.0


def test_a_genuinely_full_window_still_reads_full():
    usage_store.record(_event(1.0, 1.0))
    snap = usage_store.snapshot()
    assert _util(snap, "five_hour") == 100.0
    assert _util(snap, "seven_day") == 100.0


def test_zero_is_zero_and_not_missing():
    usage_store.record(_event(0.0, 0.0))
    snap = usage_store.snapshot()
    assert _util(snap, "five_hour") == 0.0
    assert _util(snap, "seven_day") == 0.0


def test_stored_percent_never_rescales():
    assert usage_store._stored_percent(1.0) == 1.0
    assert usage_store._stored_percent(0.4) == 0.4
    assert usage_store._stored_percent(84.0) == 84.0
    assert usage_store._stored_percent(150.0) == 100.0   # clamped
    assert usage_store._stored_percent(-1) is None
    assert usage_store._stored_percent("84") is None
    assert usage_store._stored_percent(True) is None
