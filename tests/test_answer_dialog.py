# tests/test_answer_dialog.py
"""Unit tests for dialog.py — the guarded keypress.

No test here may press a real key: the module-global `_backend` is
replaced with a fake whose methods record their calls, and the real
ctypes boundary is never exercised. (One live press, into a scratch
console owned by the tester and nothing else, proved the real backend;
the suite itself stays hands-off.)
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import dialog


class _Backend:
    """Stands in for ConsoleBackend. Records, never presses."""

    def __init__(self, state="live", outcome="sent"):
        self.state = state
        self.outcome = outcome
        self.lookups = []
        self.presses = []

    def pid_state(self, pid):
        self.lookups.append(pid)
        return self.state

    def press_in_console(self, pid, normalized):
        self.presses.append((pid, normalized))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def backend(monkeypatch):
    fake = _Backend()
    monkeypatch.setattr(dialog, "_backend", fake)
    return fake


# --- the closed vocabulary ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("return", "return"), ("enter", "return"), ("yes", "return"),
    ("y", "return"), ("RETURN", "return"), ("  return  ", "return"),
    ("escape", "escape"), ("esc", "escape"), ("cancel", "escape"),
    ("no", "escape"), ("n", "escape"),
    ("1", "1"), ("5", "5"), ("9", "9"),
])
def test_normalize_key_accepts_the_vocabulary(raw, expected):
    assert dialog.normalize_key(raw) == expected


@pytest.mark.parametrize("raw", [
    "", " ", "0", "10", "12", "return please", "yes please", "es",
    "rm -rf /", "a", None, 5, b"return",
])
def test_normalize_key_refuses_everything_else(raw):
    assert dialog.normalize_key(raw) is None


def test_spoken_key_names_what_gets_sent():
    assert dialog.spoken_key("return") == "Return"
    assert dialog.spoken_key("escape") == "Escape"
    assert dialog.spoken_key("3") == "3"


# --- answer -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_key_is_refused_before_anything_is_touched(backend):
    assert await dialog.answer(4242, "please press enter") == \
        dialog.BAD_KEY
    assert await dialog.answer(4242, "") == dialog.BAD_KEY
    assert backend.lookups == [] and backend.presses == []


@pytest.mark.asyncio
async def test_unparseable_pid_is_no_tty(backend):
    for bad in (0, -7, "x", None):
        assert await dialog.answer(bad, "return") == dialog.NO_TTY
    assert backend.lookups == []


@pytest.mark.asyncio
async def test_dead_pid_is_no_tty(backend):
    backend.state = "dead"
    assert await dialog.answer(4242, "escape") == dialog.NO_TTY
    assert backend.presses == []


@pytest.mark.asyncio
async def test_denied_pid_is_not_permitted(backend):
    backend.state = "denied"
    assert await dialog.answer(4242, "escape") == \
        dialog.NOT_PERMITTED
    assert backend.presses == []


@pytest.mark.asyncio
async def test_each_backend_outcome_maps_through(backend):
    for outcome, expected in (
            ("sent", dialog.SENT),
            ("not_found", dialog.NOT_FOUND),
            ("not_permitted", dialog.NOT_PERMITTED),
            ("failed", dialog.FAILED)):
        backend.outcome = outcome
        assert await dialog.answer(4242, "1") == expected
    assert backend.presses == [(4242, "1")] * 4


@pytest.mark.asyncio
async def test_unexpected_outcome_is_failed_not_trusted(backend):
    backend.outcome = "ok"
    assert await dialog.answer(4242, "return") == dialog.FAILED


@pytest.mark.asyncio
async def test_a_raising_backend_is_failed_never_raised(backend):
    backend.outcome = OSError("conhost is gone")
    assert await dialog.answer(4242, "return") == dialog.FAILED


@pytest.mark.asyncio
async def test_slow_lookup_is_failed_within_budget(backend, monkeypatch):
    def hang(pid):
        import time as _time
        _time.sleep(3)
        return "live"
    monkeypatch.setattr(backend, "pid_state", hang)
    monkeypatch.setattr(dialog, "LOOKUP_TIMEOUT", 0.05)
    assert await dialog.answer(4242, "return") == dialog.FAILED


@pytest.mark.asyncio
async def test_slow_press_is_failed_within_budget(backend, monkeypatch):
    def hang(pid, normalized):
        import time as _time
        _time.sleep(3)
        return "sent"
    monkeypatch.setattr(backend, "press_in_console", hang)
    monkeypatch.setattr(dialog, "SEND_TIMEOUT", 0.05)
    assert await dialog.answer(4242, "return") == dialog.FAILED
