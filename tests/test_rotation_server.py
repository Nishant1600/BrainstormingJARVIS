"""Server-side rotation: the swap happens at a pause, and never silently.

Two things are being pinned here. First, a generation is never replaced
without a trace: the outgoing brain is asked for its own handover, and if it
will not or cannot give one, the server writes a minimal entry itself and
rotates anyway. Second, the request for that handover runs with
origin="system", so the acting-tool gate refuses any write the brain might
attempt while answering it.

No test here spawns a real `claude`: the brain is a fake throughout.
"""

import asyncio
import importlib

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


class _Brain:
    def __init__(self, pending=True, overdue=False, rotates=True):
        self.rotation_pending = pending
        self.rotation_overdue = overdue
        self.rotated_with = None
        self.rotations = 0
        self.asked = []
        self.ready = True
        # The real Brain exposes this: None means no turn is in flight, which
        # is what makes the moment a pause.
        self.current_origin = None
        self.stopped = False
        self._rotates = rotates

    async def turn(self, text, origin="user", on_delta=None, on_tool=None):
        self.asked.append((text, origin))
        import brain
        return brain.TurnResult(origin, "We fixed chitauri and Tony chose Postgres.",
                                "result")

    async def rotate(self, handover=None):
        self.rotations += 1
        if not self._rotates:
            return False
        self.rotated_with = handover
        self.rotation_pending = False
        return True

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_a_pending_rotation_asks_for_a_journal_then_rotates(wired, monkeypatch):
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.asked, "the outgoing brain is asked for a handover"
    assert b.asked[0][1] == "system", "the journal request is not a user turn"
    assert "chitauri" in (b.rotated_with or "")

    import jarvis_memory as jm
    assert "chitauri" in (jm.latest_journal() or ""), "the journal is on disk"


class _Speech:
    """Records what was said, without a real SpeechScheduler: this is
    testing that the server WIRES the announcement, not the scheduler's own
    behaviour (that lives in test_speech.py against the real thing)."""

    def __init__(self):
        self.said = []

    async def say(self, text, priority=None, immediate=None):
        self.said.append((text, priority, immediate))


@pytest.mark.asyncio
async def test_a_rotation_is_shown_on_the_orb_and_never_spoken(wired, monkeypatch):
    """The user sees the brain process swap underneath him ("that's weird why
    did they just randomly restart"). It was once announced with a spoken
    line afterwards; he found that annoying the moment he knew what it was,
    and he was right. So the orb carries a "compacting" state for the
    duration, is put back to idle when it is over, and nothing is said."""
    server = wired
    b = _Brain()
    sp = _Speech()
    frames = []

    async def emit(msg):
        frames.append(msg)

    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)
    monkeypatch.setattr(server, "_voice_emit", emit)

    await server._maybe_rotate()

    assert sp.said == [], f"nothing is spoken about a rotation, but heard: {sp.said}"
    states = [f["state"] for f in frames if f.get("type") == "status"]
    assert states[0] == "compacting", states
    assert states[-1] == "idle", "the orb must be put back when it is over"


@pytest.mark.asyncio
async def test_a_failed_rotation_says_nothing(wired, monkeypatch):
    """rotate() returning False means the old brain is still serving --
    nothing happened from the user's point of view, so nothing is said."""
    server = wired
    b = _Brain(rotates=False)
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)

    await server._maybe_rotate()

    assert sp.said == []


@pytest.mark.asyncio
async def test_rotation_without_a_speech_scheduler_does_not_raise(wired, monkeypatch):
    """Boot order: rotation logic must tolerate speech being None (as it is
    before start_brain_and_speech has run)."""
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)

    await server._maybe_rotate()   # must not raise

    assert b.rotated_with is not None


@pytest.mark.asyncio
async def test_nothing_happens_when_no_rotation_is_pending(wired, monkeypatch):
    server = wired
    b = _Brain(pending=False)
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotated_with is None and b.asked == []


@pytest.mark.asyncio
async def test_a_brain_that_will_not_write_a_journal_still_rotates(wired, monkeypatch):
    """A generation must never vanish without a trace, and a silent brain must
    not block rotation forever."""
    server = wired
    b = _Brain()

    async def refuse(text, origin="user", on_delta=None):
        import brain
        return brain.TurnResult(origin, "", "timeout")

    b.turn = refuse
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None, \
        "the server wrote a minimal entry"
    assert jm.latest_journal() is None, \
        "but it is a tombstone, not something to hand the next generation"
    assert b.rotation_pending is False, "rotation happened anyway"


@pytest.mark.asyncio
async def test_an_error_turn_is_not_persisted_as_a_handover(wired, monkeypatch):
    """A failed turn may still carry the CLI's error text. That is not a
    handover and must not be fed to the next generation as one."""
    server = wired
    b = _Brain()

    async def errored(text, origin="user", on_delta=None):
        import brain
        return brain.TurnResult(origin, "API Error: overloaded_error", "error")

    b.turn = errored
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotated_with is None, "no handover carried forward"
    assert b.rotation_pending is False, "but it still rotated"
    import jarvis_memory as jm
    assert "API Error" not in (jm.latest_journal() or "")


@pytest.mark.asyncio
async def test_rotation_waits_while_another_turn_is_in_flight(wired, monkeypatch):
    """A pause means nothing is being served. Mid-conversation is not a pause."""
    server = wired
    b = _Brain()
    b.current_origin = "user"
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.asked == [] and b.rotations == 0
    assert b.rotation_pending is True, "still pending, for the next real pause"


@pytest.mark.asyncio
async def test_an_overdue_rotation_happens_even_mid_conversation(wired, monkeypatch):
    """A conversation that never pauses still has to rotate eventually."""
    server = wired
    b = _Brain(overdue=True)
    b.current_origin = "user"
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotation_pending is False


@pytest.mark.asyncio
async def test_two_pauses_at_once_produce_one_rotation(wired, monkeypatch):
    """_handle_utterance runs as a task per utterance, so two can reach the
    pause together. That must not buy two handovers and two process swaps."""
    server = wired
    b = _Brain()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(text, origin="user", on_delta=None):
        b.asked.append((text, origin))
        started.set()
        await release.wait()
        import brain
        return brain.TurnResult(origin, "handover", "result")

    b.turn = slow
    monkeypatch.setattr(server, "brain_instance", b)

    first = asyncio.create_task(server._maybe_rotate())
    # Bounded: if the handover is never asked for at all, this test must fail
    # on its assertions rather than hang the suite.
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    second = asyncio.create_task(server._maybe_rotate())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)

    assert len(b.asked) == 1, "the brain is asked once, not twice"
    assert b.rotations == 1, "the process is swapped once, not twice"


@pytest.mark.asyncio
async def test_a_failed_rotation_does_not_re_ask_at_every_pause(wired, monkeypatch):
    """rotate() returning False leaves rotation_pending True. Retrying is
    right; spending another brain turn and another journal entry on every
    utterance until it succeeds is not."""
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()
    await server._maybe_rotate()
    await server._maybe_rotate()

    assert len(b.asked) == 1, "the handover already paid for is reused"
    assert b.rotations == 3, "but rotation is still retried at each pause"


@pytest.mark.asyncio
async def test_a_journal_write_failure_does_not_stop_the_rotation(wired, monkeypatch):
    """Journalling is bookkeeping. A read-only disk must not be able to pin the
    context window open."""
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)

    def boom(text, reason="shutdown"):
        raise OSError("read-only file system")

    monkeypatch.setattr(server.jarvis_memory, "write_journal", boom)

    await server._maybe_rotate()

    assert b.rotation_pending is False


@pytest.mark.asyncio
async def test_shutdown_writes_a_journal(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "brain_instance", _Brain(pending=False))
    monkeypatch.setattr(server, "speech", None)

    await server.stop_brain_and_speech()

    import jarvis_memory as jm
    assert jm.latest_journal() is not None


@pytest.mark.asyncio
async def test_shutdown_journals_even_when_the_brain_never_started(wired, monkeypatch):
    """Autostart off, or a brain that failed to boot: there is still a shutdown
    to record, and the entry is the only trace of it."""
    server = wired
    monkeypatch.setattr(server, "brain_instance", None)
    monkeypatch.setattr(server, "speech", None)

    await server.stop_brain_and_speech()

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None
    assert jm.latest_journal() is None, "a tombstone is not a handover"


@pytest.mark.asyncio
async def test_a_journal_failure_cannot_prevent_shutdown(wired, monkeypatch):
    server = wired
    b = _Brain(pending=False)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(server.jarvis_memory, "write_journal", boom)

    await server.stop_brain_and_speech()

    assert b.stopped, "the brain was still stopped"
    assert server.brain_instance is None


@pytest.mark.asyncio
async def test_a_wedged_brain_cannot_hold_shutdown_open(wired, monkeypatch):
    """The brain's own turn timeout is 90s. Shutdown does not wait that long."""
    server = wired
    b = _Brain(pending=False)

    async def never(text, origin="user", on_delta=None):
        await asyncio.sleep(3600)

    b.turn = never
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "SHUTDOWN_JOURNAL_TIMEOUT", 0.05)

    await asyncio.wait_for(server.stop_brain_and_speech(), timeout=5.0)

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None
    assert jm.latest_journal() is None, "a tombstone is not a handover"
    assert b.stopped


# ---------------------------------------------------------------------------
# Boot: the other end of the handover. A restart must pick up where the last
# generation left off, and be told who is working right now.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_wires_the_brain_to_the_active_projects(wired, monkeypatch):
    server = wired
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")     # never spawn a real claude

    await server.start_brain_and_speech()
    try:
        assert server.brain_instance is not None
        assert server.brain_instance.active_projects is server._active_project_names
    finally:
        await server.stop_brain_and_speech()


@pytest.mark.asyncio
async def test_boot_selects_the_brain_backend_by_runtime(wired, monkeypatch):
    """JARVIS_RUNTIME=opencode boots the session-per-turn brain; anything
    else (including unset) boots the long-lived claude one. Neither is
    spawned here — autostart stays off, so this pins the selection only."""
    import brain
    server = wired
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")

    monkeypatch.setenv("JARVIS_RUNTIME", "opencode")
    importlib.reload(server)
    await server.start_brain_and_speech()
    try:
        assert isinstance(server.brain_instance, brain.OpencodeBrain)
    finally:
        await server.stop_brain_and_speech()

    monkeypatch.delenv("JARVIS_RUNTIME")
    importlib.reload(server)
    await server.start_brain_and_speech()
    try:
        assert type(server.brain_instance) is brain.Brain
    finally:
        await server.stop_brain_and_speech()


def test_active_project_names_is_empty_before_the_watcher_polls(wired, monkeypatch):
    """Boot order puts the brain before the watcher, so this must degrade to
    an empty list rather than raise."""
    server = wired
    monkeypatch.setattr(server, "session_watcher", None)
    assert server._active_project_names() == []


def test_active_project_names_skips_finished_and_never_started_sessions(wired,
                                                                        monkeypatch):
    """`gone` conversations linger in the snapshot for ten minutes so a
    completion can still be announced; a `fresh` window has never been
    prompted. Neither is work in progress."""
    import session_watch
    server = wired

    def state(project, st):
        return session_watch.SessionState(
            session_id=f"{project}-{st}", project=project, cwd="/tmp", state=st)

    class _Watcher:
        snapshot = session_watch.Snapshot(sessions=[
            state("chitauri", session_watch.WORKING),
            state("jarvis", session_watch.NEEDS_YOU),
            state("old-thing", session_watch.GONE),
            state("never-used", session_watch.FRESH),
        ])

    monkeypatch.setattr(server, "session_watcher", _Watcher())

    assert server._active_project_names() == ["chitauri", "jarvis"]


# ── the pause the user sits through ─────────────────────────────────────────
# Collecting a handover and swapping the process takes seconds during which
# nothing answers. The line spoken afterwards explains it too late: by then
# the user has already watched a dead orb and assumed a crash.

@pytest.mark.asyncio
async def test_the_pause_is_announced_before_it_starts(wired, monkeypatch):
    server = wired
    frames = []

    async def _emit(msg):
        frames.append(dict(msg))

    monkeypatch.setattr(server, "brain_instance", _Brain())
    monkeypatch.setattr(server, "speech", _Speech())
    monkeypatch.setattr(server, "_voice_emit", _emit)

    await server._maybe_rotate()

    notices = [f for f in frames if f.get("type") == "notice"]
    assert notices, "the user was given no warning at all"
    assert notices[0]["text"] == server.ROTATION_BUSY_LINE
    assert notices[-1]["text"] == "", "the banner must be cleared afterwards"


@pytest.mark.asyncio
async def test_the_busy_banner_is_shown_and_never_spoken(wired, monkeypatch):
    """The banner is for the screen. Reading it aloud would be the very
    announcement the user asked not to hear."""
    server = wired
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", _Brain())
    monkeypatch.setattr(server, "speech", sp)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: asyncio.sleep(0))

    await server._maybe_rotate()

    spoken = [t for t, _p, _i in sp.said]
    assert server.ROTATION_BUSY_LINE not in spoken
    assert spoken == []


@pytest.mark.asyncio
async def test_a_client_that_has_gone_away_cannot_stop_a_rotation(wired, monkeypatch):
    """The notice is a courtesy; the rotation is not optional."""
    server = wired
    b = _Brain()

    async def _boom(msg):
        raise ConnectionError("no voice client connected")

    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", _Speech())
    monkeypatch.setattr(server, "_voice_emit", _boom)

    await server._maybe_rotate()

    assert b.rotations == 1, "the rotation must happen whether or not anyone is listening"
