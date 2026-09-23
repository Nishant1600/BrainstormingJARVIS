"""What JARVIS says (and logs) when the brain has given up.

Before this, brain_instance.failed produced only "My language systems are
down, sir." -- true, but useless: it names neither cause nor remedy. A real
incident (an expired Claude Code login) cost the user twenty minutes of
investigation to find what was printed plainly in the server log the whole
time. Now an auth-classified failure (brain.py's `failure_reason == "auth"`)
gets a spoken line naming the actual remedy, and the remedy is also logged
at ERROR level so it's visible in the terminal without waiting on speech.

No test here spawns a real `claude` or a real brain: brain_instance and
speech are both fakes, following the pattern in test_run_announcements.py.
"""

import importlib

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import server as server_module
    importlib.reload(server_module)
    return server_module


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def say(self, text, priority=None, **k):
        self.calls.append((text, priority))


class FakeBrain:
    """Only the attributes _handle_utterance / _on_brain_state actually touch."""
    def __init__(self, ready=False, failed=False, failure_reason=None):
        self.ready = ready
        self.failed = failed
        self.failure_reason = failure_reason


# --- _on_brain_state: the "failed" event ------------------------------------

@pytest.mark.asyncio
async def test_auth_failure_speaks_the_login_remedy(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    await server._on_brain_state("failed", {"failure_reason": "auth"})

    assert speech.calls
    text, priority = speech.calls[0]
    assert "claude" in text.lower()
    assert "log in" in text.lower()
    assert priority == server.Priority.URGENT
    assert ".py" not in text and "/" not in text   # no file path or stack trace read aloud


@pytest.mark.asyncio
async def test_non_auth_failure_keeps_the_generic_line(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    await server._on_brain_state("failed", {})

    assert speech.calls
    text, priority = speech.calls[0]
    assert text == "My language systems are down, sir. Check the server log."
    assert priority == server.Priority.URGENT


@pytest.mark.asyncio
async def test_on_brain_state_never_raises_with_no_speech(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", None)
    await server._on_brain_state("failed", {"failure_reason": "auth"})
    await server._on_brain_state("failed", {})
    await server._on_brain_state("restarting", {})
    await server._on_brain_state("rate_limited", {"resets_at": 0})


@pytest.mark.asyncio
async def test_auth_failure_logs_the_remedy_at_error_level(wired, monkeypatch, caplog):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    with caplog.at_level("ERROR", logger="jarvis"):
        await server._on_brain_state("failed", {"failure_reason": "auth"})
    messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("claude" in m.lower() and "log in" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_auth_failure_logs_the_remedy_even_with_no_speech(wired, monkeypatch, caplog):
    """The whole point: visible in the terminal even if TTS itself is down."""
    server = wired
    monkeypatch.setattr(server, "speech", None)
    with caplog.at_level("ERROR", logger="jarvis"):
        await server._on_brain_state("failed", {"failure_reason": "auth"})
    messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("claude" in m.lower() and "log in" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_non_auth_failure_does_not_log_the_auth_remedy(wired, monkeypatch, caplog):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    with caplog.at_level("ERROR", logger="jarvis"):
        await server._on_brain_state("failed", {})
    messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert not any("run `claude`" in m for m in messages)


# --- _handle_utterance: a turn attempted while the brain is not ready -------

@pytest.mark.asyncio
async def test_handle_utterance_speaks_login_remedy_on_auth_failure(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance",
                        FakeBrain(ready=False, failed=True, failure_reason="auth"))

    await server._handle_utterance("are you there")

    assert speech.calls
    text, priority = speech.calls[0]
    assert "claude" in text.lower() and "log in" in text.lower()


@pytest.mark.asyncio
async def test_handle_utterance_keeps_generic_line_on_non_auth_failure(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance",
                        FakeBrain(ready=False, failed=True, failure_reason=None))

    await server._handle_utterance("are you there")

    assert speech.calls
    text, _ = speech.calls[0]
    assert text == "My language systems are down, sir."


@pytest.mark.asyncio
async def test_handle_utterance_never_raises_with_no_speech(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "brain_instance",
                        FakeBrain(ready=False, failed=True, failure_reason="auth"))
    await server._handle_utterance("are you there")   # must not raise
