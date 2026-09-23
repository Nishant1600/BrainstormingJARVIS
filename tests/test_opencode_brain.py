# tests/test_opencode_brain.py
"""OpencodeBrain: the voice brain on `opencode run --format json` children.

The state machine (generations, rotation budgets, handover wrapping, taint,
restart budgets, state callbacks) is the base Brain's and is covered by
test_brain.py. These tests cover only the transport: argv construction,
session capture, per-turn children, usage translation, warm-up semantics,
timeout/death handling, rotation onto a new session, and fail-fast model
resolution — all through a fake binary, no provider quota spent.
"""
import asyncio
import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fake(tmp_path: Path) -> str:
    """A stand-in `opencode` binary driven by environment (passed through
    untouched by the opencode runtime): FAKEBRAIN_TEXT/TOOLS/TOKENS shape
    the stream, FAKEBRAIN_ERROR makes it an error turn, FAKEBRAIN_EXIT sets
    the exit code, FAKEBRAIN_SLEEP stalls it, FAKEBRAIN_SID names the
    session, FAKEBRAIN_MARKER records the latest argv and FAKEBRAIN_LOG
    appends every invocation's argv."""
    script = tmp_path / "fake_opencode_brain.py"
    # NB: every "\n" below is a REAL newline ending a generated line. The
    # generated script itself never contains a backslash-n: it uses
    # chr(10), because a literal newline inside one of its string literals
    # is a SyntaxError in the child (found the loud way).
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "NL = chr(10)\n"
        "time.sleep(float(os.getenv('FAKEBRAIN_SLEEP', '0')))\n"
        "sid = os.getenv('FAKEBRAIN_SID', 'ses_fake0001')\n"
        "argv = sys.argv[1:]\n"
        "marker = os.getenv('FAKEBRAIN_MARKER')\n"
        "if marker:\n"
        "    open(marker, 'w', encoding='utf-8').write(json.dumps(argv))\n"
        "log = os.getenv('FAKEBRAIN_LOG')\n"
        "if log:\n"
        "    open(log, 'a', encoding='utf-8').write(json.dumps(argv) + NL)\n"
        "def ev(t, part):\n"
        "    return {'type': t, 'timestamp': 1, 'sessionID': sid, 'part': part}\n"
        "def emit(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + NL)\n"
        "for i, name in enumerate(x for x in "
        "os.getenv('FAKEBRAIN_TOOLS', '').split(',') if x):\n"
        "    emit(ev('tool_use', {'type': 'tool', 'tool': name,\n"
        "        'callID': 'c%d' % i,\n"
        "        'state': {'status': 'completed', 'input': {}}}))\n"
        "err = os.getenv('FAKEBRAIN_ERROR')\n"
        "if err:\n"
        "    emit({'type': 'error', 'timestamp': 1, 'sessionID': sid,\n"
        "        'error': {'name': 'E', 'data': {'message': err}}})\n"
        "else:\n"
        "    emit(ev('text', {'type': 'text',\n"
        "        'text': os.getenv('FAKEBRAIN_TEXT', 'OK')}))\n"
        "tin, tout, trd, twr = [int(x) for x in "
        "os.getenv('FAKEBRAIN_TOKENS', '100,10,5,0').split(',')]\n"
        "emit(ev('step_finish', {'type': 'step-finish', 'reason': 'stop',\n"
        "    'tokens': {'input': tin, 'output': tout, 'reasoning': 0,\n"
        "    'cache': {'read': trd, 'write': twr}}, 'cost': 0}))\n"
        "sys.stdout.flush()\n"
        "sys.exit(int(os.getenv('FAKEBRAIN_EXIT', '0')))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def isol(monkeypatch, tmp_path):
    """An environment the brain cannot mistake for the developer's own."""
    for var in ("JARVIS_BRAIN_MODEL", "JARVIS_RUN_MODEL", "JARVIS_OPENCODE_PATH",
                "FAKEBRAIN_TEXT", "FAKEBRAIN_TOOLS", "FAKEBRAIN_TOKENS",
                "FAKEBRAIN_ERROR", "FAKEBRAIN_EXIT", "FAKEBRAIN_SLEEP",
                "FAKEBRAIN_SID", "FAKEBRAIN_MARKER", "FAKEBRAIN_LOG"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _config(tmp_path, **kw):
    import brain
    return brain.BrainConfig(
        home=tmp_path / "jarvis",
        model=kw.pop("model", "test/model"),
        opencode_path=sys.executable,
        opencode_argv_prefix=[_fake(tmp_path)],
        turn_timeout=kw.pop("turn_timeout", 5.0),
        warmup_timeout=kw.pop("warmup_timeout", 5.0),
        **kw)


def _brain(tmp_path, **kw):
    import brain
    return brain.OpencodeBrain(_config(tmp_path, **kw))


# --- construction ------------------------------------------------------

def test_argv_shape_on_a_fresh_session(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol, model="test/model"))
    cmd = b._argv("hello")
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("fake_opencode_brain.py")
    assert cmd[2:5] == ["run", "--format", "json"]
    assert "--title" in cmd and cmd[cmd.index("--title") + 1] == "jarvis-brain"
    assert "--session" not in cmd
    assert cmd[cmd.index("--model") + 1] == "test/model"
    assert "--auto" in cmd
    assert cmd[cmd.index("--dir") + 1].endswith("jarvis")
    assert cmd[-1] == "hello"


def test_argv_continues_the_session_and_drops_the_title(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol))
    b._sid = "ses_abc"
    cmd = b._argv("again")
    assert cmd[cmd.index("--session") + 1] == "ses_abc"
    assert "--continue" in cmd
    assert "--title" not in cmd
    assert cmd[-1] == "again"


def test_model_needs_provider_slash(isol):
    import brain
    assert brain.OpencodeBrain(_config(isol, model="test/m"))._resolve_model() \
        == "test/m"
    assert brain.OpencodeBrain(_config(isol, model="sonnet"))._resolve_model() \
        is None


# --- lifecycle -----------------------------------------------------------

@pytest.mark.asyncio
async def test_start_warms_up_and_becomes_ready(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        assert b.ready and b.session_id == "ses_fake0001"
        assert b.model_in_use == "test/model"
        assert b.context_tokens == 100 + 5  # input + cache_read, translated
        assert b.baseline_tokens == b.context_tokens  # the resident floor
    finally:
        await b.stop()
    assert not b.running


@pytest.mark.asyncio
async def test_warmup_message_carries_launch_context(isol, monkeypatch):
    import brain
    log = isol / "argv.log"
    monkeypatch.setenv("FAKEBRAIN_LOG", str(log))
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
    finally:
        await b.stop()
    first = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    message = first[-1]
    assert "brain generation 1" in message
    assert "Warm-up. Reply with exactly: OK" in message


@pytest.mark.asyncio
async def test_missing_model_fails_fast_and_loud(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol, model="sonnet"))
    assert await b.start() is False
    assert b.failed


@pytest.mark.asyncio
async def test_turn_streams_text_tools_and_usage(isol, monkeypatch):
    import brain
    monkeypatch.setenv("FAKEBRAIN_TOOLS", "bash")
    monkeypatch.setenv("FAKEBRAIN_TEXT", "printed it")
    monkeypatch.setenv("FAKEBRAIN_TOKENS", "200,20,30,1")
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        seen_deltas, tooled = [], []
        r = await b.turn("run it", origin="user",
                         on_delta=seen_deltas.append,
                         on_tool=lambda: tooled.append(True))
        assert r.stop_reason == "result" and r.text == "printed it"
        assert r.tools == ["bash"] and tooled == [True]
        assert seen_deltas == ["printed it"]
        # Per-turn window, not a running total: the prompt as sent on THIS
        # turn (base _Turn.context_tokens semantics, kept).
        assert r.context_tokens == 200 + 30
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_error_turn_keeps_the_session_serving(isol, monkeypatch):
    """A failed turn must not cost the conversation: the session id stays,
    the brain stays ready, and the next turn goes out on it."""
    import brain
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        # Set only after warm-up: the fake answers every spawn from the
        # same environment, warm-up included.
        monkeypatch.setenv("FAKEBRAIN_ERROR", "boom happened")
        r = await b.turn("do it")
        assert r.stop_reason == "error" and r.error == "boom happened"
        assert b.ready and b.session_id == "ses_fake0001"
        monkeypatch.delenv("FAKEBRAIN_ERROR")
        r2 = await b.turn("again")
        assert r2.stop_reason == "result" and r2.text == "OK"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_auth_failure_is_fatal(isol, monkeypatch):
    import brain
    states = []
    b = brain.OpencodeBrain(_config(isol))
    b.on_state(lambda s, info: states.append(s))
    monkeypatch.setenv("FAKEBRAIN_ERROR",
                       "Failed to authenticate: oauth session expired")
    try:
        assert await b.start() is False
        assert b.failed and b.failure_reason == "auth"
        assert "failed" in states
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_rate_limit_error_gates_the_next_turn(isol, monkeypatch):
    import brain
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        monkeypatch.setenv("FAKEBRAIN_ERROR", "429 rate limit exceeded")
        r = await b.turn("do it")
        assert r.stop_reason == "error"
        assert b.rate_limit is not None
        r2 = await b.turn("again")
        assert r2.stop_reason == "rate_limited"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stuck_turn_times_out_but_keeps_the_session(isol, monkeypatch):
    import brain
    b = brain.OpencodeBrain(_config(isol, turn_timeout=0.5,
                                    warmup_timeout=5.0))
    try:
        assert await b.start() is True
        # Stall only the turn under test, not the warm-up before it.
        monkeypatch.setenv("FAKEBRAIN_SLEEP", "30")
        r = await b.turn("hang on")
        assert r.stop_reason == "timeout"
        assert b.session_id == "ses_fake0001"  # the conversation survived
    finally:
        await b.stop()
    assert not b.running


@pytest.mark.asyncio
async def test_stop_keeps_the_session_for_the_next_start(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol))
    assert await b.start() is True
    gen = b.generation
    await b.stop()
    assert not b.running and b.session_id == "ses_fake0001"
    assert await b.start() is True  # re-warms the same conversation
    assert b.generation == gen and b.ready
    await b.stop()


@pytest.mark.asyncio
async def test_taint_folds_into_the_generation(isol, monkeypatch):
    import brain
    monkeypatch.setenv("FAKEBRAIN_TOOLS", "read")
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True

        def tool():
            b.mark_untrusted_content("a file in one of your projects")

        await b.turn("read it", on_tool=tool)
        assert b.generation_untrusted_source == \
            "a file in one of your projects"
    finally:
        await b.stop()


# --- rotation --------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotate_starts_a_new_session_with_the_handover(isol,
                                                             monkeypatch):
    import brain
    log = isol / "argv.log"
    monkeypatch.setenv("FAKEBRAIN_LOG", str(log))
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        assert await b.rotate("note about turtles") is True
        assert b.generation == 2 and b.ready
        messages = [json.loads(line)[-1]
                    for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(messages) == 2
        assert "note about turtles" in messages[1]
        assert "brain generation 2" in messages[1]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_failed_rotation_keeps_serving(isol):
    import brain
    b = brain.OpencodeBrain(_config(isol))
    try:
        assert await b.start() is True
        gen = b.generation
        b._opencode = "/nonexistent/opencode-binary"
        assert await b.rotate("note") is False
        assert b.ready and b.generation == gen
        assert b.session_id == "ses_fake0001"
    finally:
        await b.stop()
