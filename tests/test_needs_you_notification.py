"""The macOS fallback for a needs-you nobody was listening to.

An URGENT utterance with no transport is kept as unread and re-raised when a
client connects — which is no use at all to a user who is not in the browser
tab. So when there is genuinely no voice client, Notification Centre gets it
instead. Only then: the user must never be notified about something he just
heard spoken, and neither a batched completion nor a `fresh` session earns an
interruption.

No test here posts a real notification — `notifier.notify` is replaced in
every one, and tests/conftest.py blocks the real implementation besides.
"""

import asyncio

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    server_module.voice_clients.clear()
    return server_module


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def say(self, text, priority=None, **k):
        self.calls.append((text, priority))


def _event(kind="needs_you", **over):
    session = {"session_id": "s", "voice_name": "chitauri", "project": "chitauri",
               "state": "needs_you", "needs": "permission prompt",
               "needs_a_human_hand": True, "title": "Fix the redirect",
               "summary": "Fix the redirect", "last_text": "Shall I proceed?",
               "steerable": True}
    session.update(over.pop("session", {}))
    return {"kind": kind, "at": 1.0, "session": session, **over}


class _Notifier:
    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    async def notify(self, title, message, *, subtitle=""):
        self.calls.append((title, message, subtitle))
        return self._ok


def _fake_notifier(monkeypatch, ok=True):
    import notifier
    fake = _Notifier(ok)
    monkeypatch.setattr(notifier, "available", lambda: True)
    monkeypatch.setattr(notifier, "notify", fake.notify)
    return fake


@pytest.mark.asyncio
async def test_a_needs_you_with_no_voice_client_posts_a_notification(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)

    await server._announce_needs_you(_event())

    assert len(fake.calls) == 1, "nobody heard it spoken, so notify"
    _title, message, subtitle = fake.calls[0]
    assert "chitauri" in message or "chitauri" in subtitle, "it names the session"
    assert "permission" in message, "and says why it is waiting"


@pytest.mark.asyncio
async def test_a_needs_you_with_a_client_connected_does_not_notify(wired, monkeypatch):
    """He just heard it spoken. Do not say it to him twice."""
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)
    server.voice_clients.add(object())
    try:
        await server._announce_needs_you(_event())
    finally:
        server.voice_clients.clear()

    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_failing_notifier_does_not_break_the_announcement(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    import notifier

    async def explode(*a, **k):
        raise RuntimeError("osascript is on fire")

    monkeypatch.setattr(notifier, "available", lambda: True)
    monkeypatch.setattr(notifier, "notify", explode)

    await server._announce_needs_you(_event())      # must not raise

    assert speech.calls, "the spoken announcement still happened"


@pytest.mark.asyncio
async def test_a_notification_failure_never_reaches_the_watcher(wired, monkeypatch):
    """_on_session_event runs on the loop for the watcher thread. Nothing that
    happens downstream of it may surface as an unhandled task exception."""
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())

    import notifier

    async def explode(*a, **k):
        raise RuntimeError("osascript is on fire")

    monkeypatch.setattr(notifier, "available", lambda: True)
    monkeypatch.setattr(notifier, "notify", explode)

    server._on_session_event(_event())
    await asyncio.sleep(0.05)

    for task in list(server._bg_tasks):
        if task.done():
            assert task.exception() is None


@pytest.mark.asyncio
async def test_an_unavailable_notifier_is_not_called(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)
    import notifier
    monkeypatch.setattr(notifier, "available", lambda: False)

    await server._announce_needs_you(_event())

    assert fake.calls == []


@pytest.mark.asyncio
async def test_batched_completions_never_notify(wired, monkeypatch):
    """A notification is an interruption. A finished session has not earned one."""
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)
    server._pending_completions.clear()
    server._pending_completions.extend(["chitauri", "hammer"])

    await server._announce_batch()

    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_fresh_session_never_notifies(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)

    server._on_session_event(_event(kind="fresh"))
    await asyncio.sleep(0.05)

    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_session_text_reaches_the_notifier_verbatim(wired, monkeypatch):
    """A session name comes out of somebody else's transcript. It is handed to
    notifier.notify() as an argument, so it arrives unescaped and unspliced —
    notifier.py passes it as argv, never as AppleScript source, and this path
    must not undo that by pre-formatting it into a command."""
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    fake = _fake_notifier(monkeypatch)
    payload = '" & (do shell ' + 'script "id") & "'

    await server._announce_needs_you(_event(session={"voice_name": payload}))

    assert len(fake.calls) == 1
    _title, message, subtitle = fake.calls[0]
    assert payload in message or payload in subtitle, \
        "the hostile string arrives verbatim, neither escaped nor spliced"
