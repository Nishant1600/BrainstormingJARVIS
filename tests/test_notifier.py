# tests/test_notifier.py
"""Unit tests for notifier.py -- the native notification fallback.

These tests never post a real notification: `notifier._run` is faked
at the single subprocess seam, so the developer running the suite is not
spammed. (One live toast, sent by hand and labelled as a test, proved the
script end to end; the suite itself stays silent.)
"""
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import notifier


@pytest.mark.asyncio
async def test_successful_post_returns_true(monkeypatch):
    async def ok(*args, timeout, env=None):
        return 0, "", ""
    monkeypatch.setattr(notifier, "_run", ok)
    monkeypatch.setattr(notifier, "available", lambda: True)
    assert await notifier.notify("Title", "Message") is True


@pytest.mark.asyncio
async def test_untrusted_text_travels_in_env_never_argv(monkeypatch):
    """The injection test: attacker-shaped strings must reach the child as
    environment data, with the argv carrying only fixed flags."""
    seen = {}

    async def capture(*args, timeout, env=None):
        seen["args"] = args
        seen["env"] = env
        return 0, "", ""
    monkeypatch.setattr(notifier, "_run", capture)
    monkeypatch.setattr(notifier, "available", lambda: True)
    hostile = '"; Remove-Item C:\\ -Recurse; " & evil $env:X'
    assert await notifier.notify(hostile, hostile, subtitle=hostile) is True

    assert all(hostile not in str(a) for a in seen["args"])
    assert seen["env"][notifier._ENV_TITLE] == hostile
    assert seen["env"][notifier._ENV_MESSAGE] == hostile
    assert seen["env"][notifier._ENV_SUBTITLE] == hostile
    # ...and the script itself is fixed text that never contained them.
    decoded = base64.b64decode(seen["args"][4]).decode("utf-16-le")
    assert decoded == notifier._NOTIFY_PS
    assert hostile not in decoded


@pytest.mark.asyncio
async def test_texts_are_truncated_with_ellipsis_mark(monkeypatch):
    seen = {}

    async def capture(*args, timeout, env=None):
        seen["env"] = env
        return 0, "", ""
    monkeypatch.setattr(notifier, "_run", capture)
    monkeypatch.setattr(notifier, "available", lambda: True)
    long = "x" * 500
    assert await notifier.notify(long, long) is True
    assert seen["env"][notifier._ENV_TITLE].endswith("…")
    assert len(seen["env"][notifier._ENV_TITLE]) <= 120
    assert len(seen["env"][notifier._ENV_MESSAGE]) <= 300


@pytest.mark.asyncio
async def test_nonzero_exit_is_false(monkeypatch):
    async def fail(*args, timeout, env=None):
        return 1, "", "access denied"
    monkeypatch.setattr(notifier, "_run", fail)
    monkeypatch.setattr(notifier, "available", lambda: True)
    assert await notifier.notify("T", "M") is False


@pytest.mark.asyncio
async def test_unavailable_platform_is_false_without_spawning(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("must not spawn when unavailable")
    monkeypatch.setattr(notifier, "_run", boom)
    monkeypatch.setattr(notifier, "available", lambda: False)
    assert await notifier.notify("T", "M") is False


@pytest.mark.asyncio
async def test_a_raising_seam_still_returns_false(monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(notifier, "_run", explode)
    monkeypatch.setattr(notifier, "available", lambda: True)
    assert await notifier.notify("T", "M") is False


def test_available_needs_windows_and_a_shell(monkeypatch):
    monkeypatch.setattr(notifier.sys, "platform", "win32")
    monkeypatch.setattr(notifier.shutil, "which",
                        lambda name: "C:\\ps\\" + name if name == "powershell"
                        else None)
    assert notifier.available() is True
    monkeypatch.setattr(notifier.shutil, "which", lambda name: None)
    assert notifier.available() is False
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    assert notifier.available() is False


def test_encoded_script_round_trips_to_the_fixed_source():
    """What the child decodes is byte-identical to the reviewed source:
    no layer in between can smuggle text into it."""
    decoded = base64.b64decode(notifier._encoded_script()).decode("utf-16-le")
    assert decoded == notifier._NOTIFY_PS
