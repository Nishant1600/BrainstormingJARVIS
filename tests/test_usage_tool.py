"""Usage, out loud.

The user: "what's my session limit — are you able to see what my usage is for
my account". `usage_store` already keeps whatever the CLI last told us and
`/api/usage/limits` draws it on the dashboard; this is the same reading, said
rather than drawn.

Two rules, and both are about not making a number up.

* Absence is a state. `usage_store` preserves "never observed" as
  `utilization: None` precisely so nobody renders it as a full green gauge,
  and the spoken path must not undo that. No reading means JARVIS SAYS there
  is no reading — never zero, which is a confident falsehood the user would
  plan his day on.
* A threshold warning is not a limit. `allowed_warning` means "you have
  passed a utilisation threshold". This project has already been bitten once
  by treating one of those as being cut off — it muted JARVIS completely.

And it is spoken, so a reset time is a DAY and a clock, never a timestamp.
"""

import importlib
import time
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import usage_store
    importlib.reload(usage_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module, usage_store


def _at(**kwargs) -> float:
    return (datetime.now() + timedelta(**kwargs)).timestamp()


# A Wednesday morning in the middle of January: no daylight-saving transition
# anywhere near it, and both "later today" and "three days from now" exist.
FROZEN_NOW = datetime(2026, 1, 14, 9, 0, 0)


class _FrozenClock:
    """`time`, as far as usage_store is concerned."""

    def __init__(self, at: datetime):
        self._at = at

    def time(self) -> float:
        return self._at.timestamp()


def _freeze(monkeypatch, server, usage_store, at: datetime = FROZEN_NOW):
    """Hold still both clocks this answer is built from.

    A test that asks the machine what time it is has the machine's answer in
    its assertions. This one used to build "a reset later today" as
    `now + 30 minutes`, which is TOMORROW after 23:30 — so it went red for
    half an hour a night and green again at midnight, and a test that fails on
    the wall clock only teaches people to ignore red.

    Both clocks, because they are two: `server.datetime` decides whether a
    reset is today (`_fmt_reset`), and `usage_store`'s `time` decides whether
    the window has already rolled over and how old the reading is. Freeze one
    and leave the other and they disagree by months — the window reads as
    expired and nothing says "resets" at all.
    """
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(server, "datetime", _Now)
    monkeypatch.setattr(usage_store, "time", _FrozenClock(at))
    return at


# --- registered in all three places ---------------------------------------

def test_the_three_tool_sets_still_agree(ready):
    import brain
    import jarvis_mcp
    server, _usage = ready
    assert "usage_status" in server.TOOL_HANDLERS
    assert "mcp__jarvis__usage_status" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= {
        f"mcp__jarvis__{n}" for n in server.TOOL_HANDLERS}


def test_asking_how_much_is_left_does_not_depend_on_who_is_talking(ready):
    """It reads a file and says what it found."""
    server, _usage = ready
    assert "usage_status" not in server.ACTING_TOOLS


# --- no reading is a state, not a zero -------------------------------------

def test_with_no_observation_he_says_there_is_no_reading(ready):
    server, _usage = ready
    answer = server.tool_usage_status({})
    assert "no reading" in answer.lower()
    assert "%" not in answer, "it offered a figure it does not have"
    # And it tells the brain, in as many words, not to invent one.
    assert "do not say zero" in answer


def test_a_window_nobody_has_measured_is_not_reported_as_empty(ready):
    """Only five_hour came back. seven_day must say so, not read as 0%."""
    server, usage_store = ready
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.42,
                                          "resetsAt": _at(hours=2)}}})
    answer = server.tool_usage_status({})
    assert "42% used" in answer
    assert "7-day week: no reading." in answer
    assert "0%" not in answer


# --- what a reading sounds like -------------------------------------------

def test_both_windows_are_reported_with_their_resets(ready):
    server, usage_store = ready
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.62,
                                          "resetsAt": _at(hours=3)},
                            "seven_day": {"utilization": 0.84,
                                          "resetsAt": _at(days=3)}}})
    answer = server.tool_usage_status({})
    assert "5-hour session: 62% used" in answer
    assert "7-day week: 84% used" in answer
    assert "Measured just now." in answer


def test_a_reset_today_is_a_clock_time_with_a_preposition(ready, monkeypatch):
    server, usage_store = ready
    now = _freeze(monkeypatch, server, usage_store)
    when = now.replace(hour=23)
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.5,
                                          "resetsAt": when.timestamp()}}})
    answer = server.tool_usage_status({})
    assert "resets at 11 PM" in answer, answer
    assert str(int(when.timestamp())) not in answer, "a bare timestamp"


def test_a_reset_on_another_day_names_the_day(ready, monkeypatch):
    """"until 10 AM" is wrong, and confusing when it IS 10 AM."""
    server, usage_store = ready
    when = _freeze(monkeypatch, server, usage_store) + timedelta(days=3)
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "seven_day": {"utilization": 0.84,
                                          "resetsAt": when.timestamp()}}})
    answer = server.tool_usage_status({})
    assert when.strftime("%A") in answer
    assert "resets Saturday at 9 AM" in answer, answer
    assert str(int(when.timestamp())) not in answer, "a bare timestamp"


def test_a_stale_reading_says_it_may_have_moved(ready):
    server, usage_store = ready
    old = time.time() - (usage_store.STALE_AFTER_SEC + 3600)
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.3,
                                          "resetsAt": _at(hours=1)}}},
                       now=old)
    answer = server.tool_usage_status({})
    assert "may have moved" in answer
    assert "hour" in answer, "it must say WHEN, in words a person would use"
    assert str(int(old)) not in answer, "a bare timestamp"


def test_a_window_that_has_already_reset_is_not_read_as_current(ready):
    server, usage_store = ready
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.91,
                                          "resetsAt": time.time() - 60}}})
    answer = server.tool_usage_status({})
    assert "has since reset" in answer
    assert "unknown" in answer


# --- a threshold warning is not a limit ------------------------------------

def test_a_threshold_warning_is_never_reported_as_cut_off(ready):
    """"allowed_warning" means a threshold was passed, NOT that you are
    blocked. Turning one into "you are cut off" is a bug this project has
    already shipped once, in brain.py."""
    import brain
    server, usage_store = ready
    assert "allowed_warning" not in brain.BLOCKING_RATE_LIMIT_STATUSES
    usage_store.record({"status": "allowed_warning",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.81,
                                          "resetsAt": _at(hours=1),
                                          "status": "allowed_warning"}}})
    answer = server.tool_usage_status({})
    assert "81% used" in answer
    assert "limit" not in answer.lower()
    assert "cut off" not in answer.lower()


def test_a_real_rejection_is_reported_as_a_limit(ready):
    server, usage_store = ready
    usage_store.record({"status": "rejected", "rateLimitType": "five_hour",
                        "utilization": 1.0, "resetsAt": _at(hours=2)})
    answer = server.tool_usage_status({})
    assert "at its limit" in answer


def test_the_blocking_statuses_come_from_one_place(ready):
    """Named from brain.py's set, so the two cannot drift apart."""
    import brain
    server, _usage = ready
    assert server.BLOCKING_RATE_LIMIT_STATUSES is \
        brain.BLOCKING_RATE_LIMIT_STATUSES


# --- through the real funnel ----------------------------------------------

def test_the_answer_fits_the_brains_budget(ready):
    from fastapi.testclient import TestClient
    server, usage_store = ready
    usage_store.record({"status": "allowed",
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.62,
                                          "resetsAt": _at(hours=3)},
                            "seven_day": {"utilization": 0.84,
                                          "resetsAt": _at(days=3)},
                            "seven_day_opus": {"utilization": 0.11,
                                               "resetsAt": _at(days=3)}}})
    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "usage_status", "arguments": {}})
    body = r.json()
    assert body["ok"] is True
    assert len(body["text"]) <= server.TOOL_RESULT_CAP
    assert "7-day Opus: 11% used" in body["text"]
