import asyncio
import logging
import os
import threading
import time

import pytest

import session_watch as sw
from tests.fixtures.roster import write_roster, write_transcript


def _watcher(root):
    return sw.SessionWatcher(roots=[root], interval=0.01)


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll `predicate` for up to `timeout`s instead of one fixed sleep, so
    tests driving the real event loop are neither flaky nor slow."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def test_the_first_poll_is_silent_no_matter_what_is_running(tmp_path):
    """Starting JARVIS must not announce the fourteen sessions already open."""
    root = tmp_path / ".claude"
    for i in range(3):
        write_roster(root, pid=os.getpid(), session_id=f"s{i}", cwd=f"/p/{i}",
                     name=f"n{i}", status="waiting", waiting_for="permission prompt")
        write_transcript(root, cwd=f"/p/{i}", session_id=f"s{i}", title="T",
                         last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)

    w.poll_once()

    assert w.snapshot.sessions, "the snapshot is built"
    assert events == [], "but nothing is announced on the first poll"


def test_a_session_that_starts_needing_you_after_startup_is_announced(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once()                                        # baseline: working

    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status="waiting", waiting_for="permission prompt")
    w.poll_once()

    assert [e["kind"] for e in events] == ["needs_you"]
    assert events[0]["session"]["needs"] == "permission prompt"
    assert events[0]["session"]["needs_a_human_hand"] is True


def test_needs_you_is_announced_once_not_on_every_poll(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once()
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status="waiting", waiting_for="permission prompt")

    w.poll_once()
    w.poll_once()
    w.poll_once()

    assert len(events) == 1, "one transition, one announcement"


def test_a_session_finishing_after_real_work_is_announced(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once(now=1000.0)

    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="idle")
    w.poll_once(now=1000.0 + 45)

    assert [e["kind"] for e in events] == ["finished"]


def test_a_session_that_exits_into_unknown_is_still_announced_as_finished(tmp_path):
    """Review finding 6: a session whose roster status vanishes as it exits
    lands on `unknown` (still alive, but no status field) rather than
    cleanly on `idle`/`gone`. That must be announced too, or a session
    exiting this way is silently never reported as finished."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once(now=1000.0)

    # Still alive (pid_alive is real here and this test's own pid is alive),
    # but the roster entry now carries no status at all -- `_derive_state`
    # reports this as `unknown`, not `idle`/`gone`.
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status=None)
    w.poll_once(now=1000.0 + 45)

    assert [e["kind"] for e in events] == ["finished"]


def test_a_brief_flicker_of_work_is_not_announced(tmp_path):
    """A two-second busy blip is not a finished job."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once(now=1000.0)

    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="idle")
    w.poll_once(now=1002.0)

    assert events == []


def test_a_fresh_session_is_never_announced(tmp_path):
    """It has no transcript: nobody has spoken to it. Announcing it is noise."""
    root = tmp_path / ".claude"
    w = _watcher(root)
    events = []
    w.on_event(events.append)
    w.poll_once()

    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status="waiting", waiting_for="dialog open")
    w.poll_once()

    assert events == []
    assert w.snapshot.sessions[0].state == sw.FRESH


def test_a_gone_session_is_retained_so_the_completion_can_still_be_spoken(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    w.poll_once(now=1000.0)

    (root / "sessions" / f"{os.getpid()}.json").unlink()
    w.poll_once(now=1000.0 + 60)

    s = w.snapshot.by_id("s")
    assert s is not None and s.state == sw.GONE

    w.poll_once(now=1000.0 + 60 + sw.GONE_RETENTION_SEC + 1)
    assert w.snapshot.by_id("s") is None, "dropped after the retention window"


def test_a_gone_then_returned_session_does_not_duplicate_a_live_sessions_name(
        tmp_path, monkeypatch):
    """Review finding 8: gone-cache entries are appended to the snapshot
    AFTER `_assign_voice_names` has already run for the live sessions, so a
    stale name computed in an earlier poll (when the now-gone session was
    the only one in its project) can collide with a live session's name
    computed independently in this poll. Every session in a published
    snapshot must have been through naming TOGETHER."""
    # "new" below uses a placeholder pid (not a real process) to represent a
    # second, later conversation -- pinned alive per the `all_alive`
    # precedent in test_session_watch.py.
    monkeypatch.setattr(sw, "pid_alive", lambda pid: True)
    root = tmp_path / ".claude"
    old_pid = os.getpid()
    new_pid = os.getpid() + 1000003

    write_roster(root, pid=old_pid, session_id="old", cwd="/p/hammer",
                name="hammer-old", status="idle", started_at=1_000_000_000_000)
    write_transcript(root, cwd="/p/hammer", session_id="old", title="T",
                     last_prompt="P")
    w = _watcher(root)
    w.poll_once(now=1000.0)
    assert w.snapshot.by_id("old").voice_name == "hammer", "baseline: alone, plain name"

    # "old" exits -> GONE, cached with the voice_name from when it was alone.
    (root / "sessions" / f"{old_pid}.json").unlink()
    w.poll_once(now=1001.0)
    assert w.snapshot.by_id("old").state == sw.GONE
    assert w.snapshot.by_id("old").voice_name == "hammer"

    # A NEW conversation for the SAME project appears while "old" is still
    # within the gone-cache retention window. Named independently (as the
    # only LIVE session in its project) it would also come out "hammer".
    write_roster(root, pid=new_pid, session_id="new", cwd="/p/hammer",
                name="hammer-new", status="idle", started_at=1_800_000_000_000)
    write_transcript(root, cwd="/p/hammer", session_id="new", title="T",
                     last_prompt="P")
    w.poll_once(now=1002.0)

    old, new = w.snapshot.by_id("old"), w.snapshot.by_id("new")
    assert old is not None and new is not None
    assert old.voice_name != new.voice_name, \
        f"gone {old.voice_name!r} collides with live {new.voice_name!r}"


def test_a_failing_callback_cannot_stop_the_watcher(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    seen = []
    w.on_event(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    w.on_event(seen.append)
    w.poll_once(now=1000.0)

    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status="waiting", waiting_for="permission prompt")
    w.poll_once(now=1001.0)

    assert len(seen) == 1, "the second subscriber still hears it"


@pytest.mark.asyncio
async def test_the_watcher_loop_polls_and_stops_cleanly(tmp_path):
    import asyncio
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="idle")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)

    await w.start()
    try:
        await asyncio.sleep(0.05)
        assert w.snapshot.sessions
    finally:
        await w.stop()
    assert w._task is None


@pytest.mark.asyncio
async def test_a_subscriber_that_schedules_a_task_succeeds_on_the_real_loop(tmp_path):
    """Finding 1 regression.

    _publish() used to call subscriber callbacks synchronously from
    poll_once(), and _loop() drives poll_once() via
    asyncio.to_thread(...) -- a WORKER thread. A subscriber that calls
    asyncio.create_task() there raised RuntimeError("no running event
    loop"), and _publish()'s own try/except swallowed it, so the watcher
    looked healthy while no event was ever delivered.

    This must drive the REAL loop with start()/stop(), not call
    poll_once() directly -- the sync tests above call poll_once() from the
    test thread itself and cannot see this bug.
    """
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)

    received = []
    errors = []
    spawned_tasks = []

    def on_event(event):
        try:
            spawned_tasks.append(asyncio.create_task(asyncio.sleep(0)))
        except RuntimeError as e:
            errors.append(e)
        else:
            received.append(event)

    w.on_event(on_event)

    await w.start()
    try:
        assert await _wait_for(lambda: w.snapshot.sessions), "baseline poll happened"

        write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                     status="waiting", waiting_for="permission prompt")
        assert await _wait_for(lambda: received or errors), "the transition was observed"
    finally:
        await w.stop()

    assert errors == [], f"subscriber's asyncio.create_task() failed: {errors!r}"
    assert [e["kind"] for e in received] == ["needs_you"]
    assert len(spawned_tasks) == 1
    await asyncio.gather(*spawned_tasks)          # the spawned task actually ran


@pytest.mark.asyncio
async def test_subscriber_callback_runs_on_the_event_loop_thread(tmp_path):
    """The dispatch fix must land callbacks back on the loop thread, not
    leave them running on the polling worker thread."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)

    loop_thread = threading.current_thread()
    seen_threads = []
    w.on_event(lambda e: seen_threads.append(threading.current_thread()))

    await w.start()
    try:
        assert await _wait_for(lambda: w.snapshot.sessions), "baseline poll happened"

        write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                     status="waiting", waiting_for="permission prompt")
        assert await _wait_for(lambda: len(seen_threads) >= 1), "the transition was observed"
    finally:
        await w.stop()

    assert seen_threads == [loop_thread]


def test_gone_at_bookkeeping_does_not_leak_when_a_session_returns_and_dies_again(tmp_path):
    """Finding 2 regression.

    poll_once() deleted `_gone_cache[sid]` when a conversation reappeared
    but never popped `_gone_at[sid]` -- a leaked float per session id that
    dies and comes back, in a process meant to run for days. Drive the
    gone -> back -> gone cycle twice and assert the bookkeeping dicts never
    grow past what a single live "gone" session needs.
    """
    root = tmp_path / ".claude"
    sock = root / "sessions" / f"{os.getpid()}.json"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")
    w = _watcher(root)
    w.poll_once(now=1000.0)
    original = sock.read_bytes()

    for cycle in range(3):
        base = 1000.0 + cycle * 100
        sock.unlink()
        w.poll_once(now=base + 10)
        assert len(w._gone_cache) == 1
        assert len(w._gone_at) == 1

        sock.write_bytes(original)
        w.poll_once(now=base + 20)
        assert len(w._gone_cache) == 0, "gone_cache is popped when the session returns"
        assert len(w._gone_at) == 0, "gone_at leaked here before the fix"


@pytest.mark.asyncio
async def test_stop_logs_unexpected_errors_instead_of_swallowing_them(tmp_path, caplog):
    """Finding 3 regression: stop() used to catch (CancelledError, Exception)
    and pass silently either way. A real shutdown failure must be logged."""
    root = tmp_path / ".claude"
    w = _watcher(root)

    async def _broken_loop():
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            raise RuntimeError("boom during shutdown")

    w._task = asyncio.create_task(_broken_loop())
    await asyncio.sleep(0)      # let it reach the `await asyncio.sleep(1000)` inside the try

    with caplog.at_level(logging.WARNING, logger="session_watch"):
        await w.stop()          # must not raise, but must not stay silent

    assert w._task is None
    assert "boom during shutdown" in caplog.text or "did not stop cleanly" in caplog.text

    # safe to call again, and safe on a watcher that was never started
    await w.stop()
    await _watcher(root).stop()
