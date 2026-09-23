# tests/test_opencode_sessions.py
"""Server sessions wiring on the opencode runtime: which watcher polls,
how runs are excluded from the snapshot, and how a staged steer is
performed and spoken about.

The claude-backend behavior is pinned elsewhere (test_session_inbox.py
for the staged flow, test_rotation_server.py for boot); these tests pin
only the opencode branch, with the runtime selected through JARVIS_RUNTIME
the same way production selects it.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_RUNTIME", raising=False)
    import server as server_module
    importlib.reload(server_module)
    return server_module


class _Speech:
    def __init__(self):
        self.said = []

    async def say(self, text, *a, **k):
        self.said.append(text)

        class _Utt:
            was_cancelled = False
        return _Utt()

    async def wait_for(self, utt, timeout=60.0):
        return True

    async def open_cancel_window(self, *a, **k):
        return False


def _opencode_server(monkeypatch):
    """The server module reloaded under JARVIS_RUNTIME=opencode."""
    monkeypatch.setenv("JARVIS_RUNTIME", "opencode")
    import server as server_module
    importlib.reload(server_module)
    return server_module


# --- watcher selection ----------------------------------------------------

@pytest.mark.asyncio
async def test_opencode_runtime_polls_the_opencode_watcher(srv, monkeypatch):
    import opencode_watch
    monkeypatch.setattr(opencode_watch, "default_db_paths",
                        lambda extra=None: [])
    server = _opencode_server(monkeypatch)
    await server.start_session_watcher()
    try:
        assert isinstance(server.session_watcher,
                          opencode_watch.OpencodeWatcher)
    finally:
        await server.stop_session_watcher()
    assert server.session_watcher is None


@pytest.mark.asyncio
async def test_default_runtime_still_polls_the_roster_watcher(srv):
    import session_watch
    server = srv
    await server.start_session_watcher()
    try:
        assert type(server.session_watcher) is session_watch.SessionWatcher
    finally:
        await server.stop_session_watcher()


# --- run exclusion speaks session ids --------------------------------------

def _run_with_opencode_event(server, store, sid):
    run_id = store.create_run("do a thing", "proj", "/tmp/x", "api")
    store.append_event(run_id, 1, "step_start", json.dumps({
        "type": "step_start", "sessionID": sid, "part": {}}))
    return run_id


def test_run_exclusion_translates_run_ids_to_session_ids(srv, monkeypatch,
                                                         tmp_path):
    import run_store
    run_store.init_db()
    server = _opencode_server(monkeypatch)
    server._run_ids_cache = (0.0, frozenset())
    server._opencode_run_sids.clear()
    run_id = _run_with_opencode_event(server, run_store, "ses_abc123")
    try:
        ids = server._jarvis_run_session_ids()
        assert "ses_abc123" in ids
        assert run_id not in ids
    finally:
        server._run_ids_cache = (0.0, frozenset())
        server._opencode_run_sids.clear()


def test_run_exclusion_on_claude_keeps_run_ids(srv, monkeypatch):
    import run_store
    run_store.init_db()
    server = srv
    server._run_ids_cache = (0.0, frozenset())
    try:
        run_id = _run_with_opencode_event(server, run_store, "ses_abc123")
        assert run_id in server._jarvis_run_session_ids()
    finally:
        server._run_ids_cache = (0.0, frozenset())


def test_run_without_events_excludes_nothing_on_opencode(srv, monkeypatch):
    """A queued run has streamed nothing yet: no session to exclude, and
    crucially no crash — the lookup fails open."""
    import run_store
    run_store.init_db()
    server = _opencode_server(monkeypatch)
    server._run_ids_cache = (0.0, frozenset())
    server._opencode_run_sids.clear()
    try:
        run_id = run_store.create_run("do a thing", "proj", "/tmp/x", "api")
        assert run_id not in server._jarvis_run_session_ids()
    finally:
        server._run_ids_cache = (0.0, frozenset())
        server._opencode_run_sids.clear()


# --- performing a staged steer ----------------------------------------------

def _perform(server, monkeypatch, outcome):
    import asyncio
    calls = []
    monkeypatch.setattr(server.run_store, "record_steer",
                        lambda *a, **k: None)
    speech = _Speech()
    monkeypatch.setattr(server, "speech", speech)

    def fake_post(session_id, prompt, **k):
        calls.append((session_id, prompt))
        return outcome

    monkeypatch.setattr(server.opencode_steer, "post_to_session", fake_post)
    item = server._StagedSteer(session_id="ses_9", voice_name="chitauri",
                               project="chitauri", prompt="use Postgres",
                               socket_path=None)
    asyncio.run(server._perform_steer(item))
    return speech.said, calls


def test_opencode_steer_sends_by_session_id(srv, monkeypatch):
    server = _opencode_server(monkeypatch)
    said, calls = _perform(server, monkeypatch, server.opencode_steer.SENT)
    assert calls == [("ses_9", "use Postgres")]
    assert said[-1] == "Passed to chitauri, sir."


def test_opencode_steer_success_carries_no_approval_caveat(srv, monkeypatch):
    """There is no approval-on-receipt gate on this backend, so the
    crossSessionInbound warning would be a lie here."""
    server = _opencode_server(monkeypatch)
    said, _calls = _perform(server, monkeypatch,
                            server.opencode_steer.SENT)
    assert "approve" not in said[-1].lower()


def test_opencode_not_live_names_no_socket(srv, monkeypatch):
    server = _opencode_server(monkeypatch)
    said, _calls = _perform(server, monkeypatch,
                            server.opencode_steer.NOT_LIVE)
    assert "socket" not in said[-1]
    assert "couldn't find chitauri" in said[-1]


def test_opencode_failed_steer_is_plain_about_it(srv, monkeypatch):
    server = _opencode_server(monkeypatch)
    said, _calls = _perform(server, monkeypatch, "some_other_failure")
    assert said[-1] == "I couldn't deliver that to chitauri, sir."


# --- staging ------------------------------------------------------------------

def test_opencode_staged_note_has_no_inbox_caveat(srv, monkeypatch):
    """The staged note must not offer to enable an inbox no opencode
    session will ever check."""
    server = _opencode_server(monkeypatch)

    class _Session:
        session_id = "ses_9"
        voice_name = "chitauri"
        project = "chitauri"
        needs_a_human_hand = False
        steerable = True
        socket_path = None

    monkeypatch.setattr(server, "_resolve_or_explain",
                        lambda n: (_Session(), None, None))
    monkeypatch.setattr(server, "speech", object())
    import asyncio
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        server.tool_steer_session({"name": "chitauri", "prompt": "use Postgres"}))
    assert "staged" in out
    assert "approve" not in out
    assert "enable_session_inbox" not in out
