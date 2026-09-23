"""JARVIS's own runs are not the user's conversations.

Every `spawn_run` starts a `claude -p` process, and that process registers in
the Claude Code roster like anything else. Measured live, after two runs on
one project: "12 conversations in 9 projects" became "16 in 10", and

    steer_session {"name": "tony-starks-website"}
      -> "There are 2: the newer tony-starks-website and the older
          tony-starks-website. Which one?"

Both were dead one-shot runs the user never opened, neither steerable,
neither anything he was doing.

The correlation is exact rather than a guess: `run_executor._command` passes
the run id to the CLI as `--session-id`, so a roster session whose id is a
row in `runs` IS a run JARVIS started. Run ids are UUID4s this process
minted, so nothing else can collide with one.
"""

import importlib

import pytest

import session_watch


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module, run_store


class _Watcher:
    def __init__(self, sessions):
        self.snapshot = session_watch.Snapshot(sessions=sessions, taken_at=1.0)


def _session(session_id, project="tony-starks-website", cwd="/tmp/tsw",
             state=session_watch.IDLE):
    return session_watch.SessionState(
        session_id=session_id, cwd=cwd, project=project, state=state,
        roster_name=project, voice_name=project, steerable=True)


def test_a_run_is_kept_out_of_the_conversation_roster(wired, monkeypatch):
    server, store = wired
    run_id = store.create_run("build a site", "tony-starks-website",
                              "/tmp/tsw", "voice")
    monkeypatch.setattr(server, "session_watcher", _Watcher([
        _session("a-real-conversation"),
        _session(run_id),
    ]))
    server._run_ids_cache = (0.0, frozenset())

    ids = [s.session_id for s in server._snapshot_or_empty().sessions]

    assert ids == ["a-real-conversation"]


def test_two_dead_runs_no_longer_make_a_project_ambiguous(wired, monkeypatch):
    """The live symptom, stated as the outcome it prevents."""
    server, store = wired
    first = store.create_run("build a site", "tony-starks-website",
                             "/tmp/tsw", "voice")
    second = store.create_run("improve it", "tony-starks-website",
                              "/tmp/tsw", "voice")
    real = _session("the-users-own-window")
    monkeypatch.setattr(server, "session_watcher", _Watcher(
        [real, _session(first), _session(second)]))
    server._run_ids_cache = (0.0, frozenset())

    session, problem, _reason = server._resolve_or_explain("tony-starks-website")

    assert problem is None, f"it asked which one: {problem}"
    assert session.session_id == "the-users-own-window"


def test_list_sessions_counts_conversations_not_runs(wired, monkeypatch):
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    monkeypatch.setattr(server, "session_watcher", _Watcher([
        _session("real-one", project="chitauri", cwd="/tmp/chitauri"),
        _session(run_id),
    ]))
    server._run_ids_cache = (0.0, frozenset())

    out = server.tool_list_sessions({})

    assert "1 conversation in 1 project" in out
    assert "tony-starks-website" not in out


def test_a_run_finishing_is_not_announced_twice(wired, monkeypatch):
    """The run pipeline narrates its own runs. The watcher must not narrate
    them again, in a different vocabulary."""
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    monkeypatch.setattr(server, "session_watcher", _Watcher([_session(run_id)]))
    server._run_ids_cache = (0.0, frozenset())
    monkeypatch.setattr(server, "_spawn", lambda coro: coro.close())

    server._on_session_event({"kind": "finished",
                              "session": {"session_id": run_id,
                                          "voice_name": "tony-starks-website"}})

    assert server._pending_completions == []


def test_a_real_conversation_finishing_is_still_announced(wired, monkeypatch):
    server, _store = wired
    monkeypatch.setattr(server, "session_watcher", _Watcher([]))
    server._run_ids_cache = (0.0, frozenset())
    monkeypatch.setattr(server, "_spawn", lambda coro: coro.close())

    server._on_session_event({"kind": "finished",
                              "session": {"session_id": "real",
                                          "voice_name": "chitauri"}})

    assert server._pending_completions == ["chitauri"]


def test_run_status_still_reports_on_runs(wired, monkeypatch):
    """Runs are hidden from the CONVERSATION view, not from JARVIS."""
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    store.update_run(run_id, status=store.RunStatus.RUNNING, started_at=1.0)
    monkeypatch.setattr(server, "session_watcher", _Watcher([_session(run_id)]))
    server._run_ids_cache = (0.0, frozenset())

    assert "tony-starks-website" in server.tool_run_status({})


def test_an_unreadable_run_table_hides_nothing(wired, monkeypatch):
    """Fails OPEN: a database error must never make real conversations
    disappear."""
    server, _store = wired
    monkeypatch.setattr(server, "session_watcher", _Watcher([_session("x")]))
    server._run_ids_cache = (0.0, frozenset())

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server.run_store, "all_run_ids", boom)

    assert [s.session_id for s in server._snapshot_or_empty().sessions] == ["x"]


# ---------------------------------------------------------------------------
# The DASHBOARD paths. Same rule, same filter — the voice path having it is
# not the same as the page having it, and for a long time only one did: the
# Sessions tab, its badge, the project groups and the Needs-You panel all
# counted dead one-shot runs and disagreed with the two tabs beside them.
# ---------------------------------------------------------------------------

def _client(server, watcher):
    """A live app whose watcher is the one the test built.

    `lifespan` starts the real watcher on entry, so the fake is installed
    AFTER TestClient has entered — exactly the ordering test_specs_api uses
    for `cached_projects`.
    """
    from fastapi.testclient import TestClient
    # An Origin the dashboard itself would send: the app refuses a WebSocket
    # that did not come from a page JARVIS serves.
    c = TestClient(server.app, headers={"origin": "http://localhost:5173"})
    c.__enter__()
    server.session_watcher = watcher
    server._run_ids_cache = (0.0, frozenset())
    return c


def test_api_sessions_does_not_serve_jarvis_own_runs(wired):
    """The reviewer's reproduction: a run id came back from /api/sessions."""
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    watcher = _Watcher([_session("a-real-conversation"), _session(run_id)])
    c = _client(server, watcher)
    try:
        body = c.get("/api/sessions").json()
    finally:
        c.__exit__(None, None, None)

    ids = [s["session_id"] for s in body["sessions"]]
    assert run_id not in ids, f"a run id reached the dashboard: {ids}"
    assert ids == ["a-real-conversation"]


def test_api_sessions_project_groups_do_not_count_runs(wired):
    """The badge and the project groups are built from the same rows."""
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    watcher = _Watcher([
        _session("real", project="chitauri", cwd="/tmp/chitauri"),
        _session(run_id)])
    c = _client(server, watcher)
    try:
        body = c.get("/api/sessions").json()
    finally:
        c.__exit__(None, None, None)

    assert list(body["projects"]) == ["chitauri"]


def test_api_sessions_state_filter_still_works_after_filtering(wired):
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    watcher = _Watcher([
        _session("busy", state=session_watch.WORKING),
        _session("waiting", state=session_watch.NEEDS_YOU),
        _session(run_id, state=session_watch.WORKING)])
    c = _client(server, watcher)
    try:
        body = c.get("/api/sessions", params={"state": "working"}).json()
    finally:
        c.__exit__(None, None, None)

    assert [s["session_id"] for s in body["sessions"]] == ["busy"]


def test_ws_sessions_opening_snapshot_does_not_carry_runs(wired):
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    watcher = _Watcher([_session("a-real-conversation"), _session(run_id)])
    c = _client(server, watcher)
    try:
        with c.websocket_connect("/ws/sessions") as ws:
            first = ws.receive_json()
    finally:
        c.__exit__(None, None, None)

    assert first["type"] == "snapshot"
    ids = [s["session_id"] for s in first["sessions"]]
    assert run_id not in ids, f"a run id reached the socket: {ids}"


def test_a_runs_session_event_is_not_broadcast_to_the_dashboard(wired,
                                                                monkeypatch):
    """The third leak on the same rule.

    `sessions-live.ts` patches in the one session an `event` names without
    reconciling, so a run event puts a run ROW on the Sessions tab — even
    though the snapshot it arrived beside was clean.
    """
    server, store = wired
    run_id = store.create_run("build", "tony-starks-website", "/tmp/tsw",
                              "voice")
    monkeypatch.setattr(server, "session_watcher", _Watcher([]))
    server._run_ids_cache = (0.0, frozenset())
    sent: list[dict] = []

    class _Nothing:
        def close(self):
            pass

    def _capture(event):
        """Records the call, not the coroutine: `_spawn` is stubbed out, so a
        real `async def` here would never run its body."""
        sent.append(event)
        return _Nothing()

    monkeypatch.setattr(server, "_broadcast_session_event", _capture)
    monkeypatch.setattr(server, "_spawn", lambda coro: coro.close())

    server._on_session_event({"kind": "needs_you",
                              "session": {"session_id": run_id,
                                          "voice_name": "tony-starks-website"}})
    server._on_session_event({"kind": "needs_you",
                              "session": {"session_id": "a-real-one",
                                          "voice_name": "chitauri"}})

    ids = [e["session"]["session_id"] for e in sent]
    assert ids == ["a-real-one"], f"a run event was broadcast: {ids}"
