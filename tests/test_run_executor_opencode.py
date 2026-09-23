# tests/test_run_executor_opencode.py
"""RunExecutor on runtime="opencode": `opencode run --format json` children.

The shared machinery (permits, timeouts, cancel, terminal-state invariant)
is runtime-agnostic and covered by test_run_executor.py against the claude
path. These tests cover only what the opencode path does differently:
argv construction (list, never split; prompt positional; --auto;
--session/--continue resume), stdin DEVNULL, per-step usage/cost/turns,
text accumulation, the error-event latch, resume-session lookup off stored
events, and fail-fast model resolution.

The fake stands in for the `opencode` binary the way _fake_claude stands
in for `claude`: argv[0] is sys.executable and argv[1] is this script, so
no shell and no splitting is ever involved — which is also what makes a
binary path with spaces survive here.
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

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
RUN_OK = FIXTURES / "run_ok.jsonl"
RUN_ERR = FIXTURES / "run_err.jsonl"
RUN_BASH = FIXTURES / "run_bash.jsonl"

# The ses_* id every line of run_ok.jsonl carries.
RUN_OK_SESSION = "ses_f36308ffcffeC1Q1oj7QtwjR0m"


def _fake_opencode(tmp_path: Path, fixture: Path | None = None,
                   exit_code: int = 0, sleep_sec: float = 0.0) -> str:
    """A stand-in `opencode` binary: records argv, replays a fixture, exits.

    Behaviour is driven by environment (which the opencode runtime passes
    through untouched): FAKE_OPENCODE_LINES (JSON list of verbatim lines,
    else the fixture file), FAKE_OPENCODE_EXIT, FAKE_OPENCODE_SLEEP,
    FAKE_OPENCODE_MARKER (argv[1:] recorded as JSON).
    """
    script = tmp_path / f"fake_opencode_{len(str(fixture))}_{exit_code}.py"
    fixture_src = (f"open({str(fixture)!r}, encoding='utf-8').read()"
                   if fixture is not None else "''")
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "marker = os.getenv('FAKE_OPENCODE_MARKER')\n"
        "if marker:\n"
        "    open(marker, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "time.sleep(float(os.getenv('FAKE_OPENCODE_SLEEP', "
        f"'{sleep_sec}')))\n"
        "lines = os.getenv('FAKE_OPENCODE_LINES')\n"
        "text = '\\n'.join(json.loads(lines)) + '\\n' if lines else " + fixture_src + "\n"
        "sys.stdout.write(text)\n"
        "sys.stdout.flush()\n"
        "sys.exit(int(os.getenv('FAKE_OPENCODE_EXIT', "
        f"'{exit_code}')))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _executor(store, mod, tmp_path):
    """An opencode-runtime executor driving the fake binary by argv list.

    The model comes from JARVIS_RUN_MODEL (set by the env fixture), the
    way production relies on configuration rather than a hardcoded id.
    """
    return mod.RunExecutor(
        store, opencode_path=sys.executable,
        opencode_argv_prefix=[_fake_opencode(tmp_path, RUN_OK)])


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_RUN_MODEL", "test/model")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()
    import run_executor
    importlib.reload(run_executor)
    return run_store, run_executor, tmp_path


def _marker_argv(tmp_path, monkeypatch, name="argv.json"):
    marker = tmp_path / name
    monkeypatch.setenv("FAKE_OPENCODE_MARKER", str(marker))
    return marker


# --- construction ------------------------------------------------------

def test_command_shape(env, tmp_path, monkeypatch):
    """Binary verbatim first, `run --format json`, explicit --model, --auto
    by default, and the prompt as the trailing positional — never stdin."""
    store, mod, tmp = env
    marker = _marker_argv(tmp_path, monkeypatch)
    ex = mod.RunExecutor(store, opencode_path="/opt/Open Code/opencode",
                         opencode_argv_prefix=["--prefix-flag"])
    cmd = ex._command("run-1", None, "test/model", "do a thing")
    assert cmd[:3] == ["/opt/Open Code/opencode", "--prefix-flag", "run"]
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "test/model"
    assert "--auto" in cmd
    assert cmd[-1] == "do a thing"
    assert "--session-id" not in cmd and "-p" not in cmd


def test_command_respects_skip_permissions_opt_out(env, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("JARVIS_SKIP_PERMISSIONS", "false")
    store, _, _ = env
    import run_executor as mod
    importlib.reload(mod)
    ex = mod.RunExecutor(store, opencode_path="opencode")
    assert "--auto" not in ex._command("run-1", None, "t/m", "p")


def test_command_resume_continues_the_session(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, opencode_path="opencode")
    cmd = ex._command("run-2", "ses_abc123", "test/model", "again")
    assert cmd[cmd.index("--session") + 1] == "ses_abc123"
    assert "--continue" in cmd
    assert cmd[-1] == "again"


def test_opencode_model_resolution_order(env, monkeypatch):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, opencode_path="opencode")
    assert ex._resolve_model("explicit/m") == "explicit/m"
    monkeypatch.setenv("JARVIS_RUN_MODEL", "run/m")
    monkeypatch.setenv("JARVIS_BRAIN_MODEL", "brain/m")
    assert ex._resolve_model(None) == "run/m"
    monkeypatch.delenv("JARVIS_RUN_MODEL")
    assert ex._resolve_model(None) == "brain/m"


def test_opencode_missing_model_fails_fast(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.delenv("JARVIS_RUN_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_BRAIN_MODEL", raising=False)
    ex = mod.RunExecutor(store, opencode_path=sys.executable,
                         opencode_argv_prefix=[_fake_opencode(tmp, None)])
    with pytest.raises(ValueError, match="no model configured"):
        ex._resolve_model(None)


# --- end to end ----------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_run_reaches_succeeded_with_usage(env):
    store, mod, tmp = env
    ex = _executor(store, mod, tmp)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED
    assert run["exit_code"] == 0
    assert run["input_tokens"] == 11059
    assert run["output_tokens"] == 11
    assert run["cache_read_tokens"] == 113
    assert run["cache_creation_tokens"] == 0
    assert run["num_turns"] == 1
    assert run["result_text"] == "OK"
    events = store.get_events(run_id, limit=100)
    assert [e["kind"] for e in events] == ["step_start", "text", "step_finish"]


@pytest.mark.asyncio
async def test_explicit_model_wins_over_env(env, tmp_path, monkeypatch):
    store, mod, tmp = env
    marker = _marker_argv(tmp_path, monkeypatch, "argv4.json")
    ex = _executor(store, mod, tmp)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api",
                            model="explicit/m")
    await ex.wait_for(run_id)
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "explicit/m"
    run = await asyncio.to_thread(store.get_run, run_id)
    assert run["requested_model"] == "explicit/m"


@pytest.mark.asyncio
async def test_prompt_travels_on_argv_not_stdin(env, tmp_path, monkeypatch):
    store, mod, tmp = env
    marker = _marker_argv(tmp_path, monkeypatch)
    ex = _executor(store, mod, tmp)
    prompt = "line one\nline two with spaces"
    run_id = await ex.spawn(prompt, "proj", str(tmp), "api")
    await ex.wait_for(run_id)
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[-1] == prompt


@pytest.mark.asyncio
async def test_error_event_fails_the_run_despite_exit_zero(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(
        store, opencode_path=sys.executable,
        opencode_argv_prefix=[_fake_opencode(tmp, RUN_ERR)])
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert run["exit_code"] == 0
    assert run["is_error"] == 1
    assert "Unexpected server error" in (run["result_text"] or "")


@pytest.mark.asyncio
async def test_nonzero_exit_marks_failed(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("FAKE_OPENCODE_EXIT", "3")
    ex = _executor(store, mod, tmp)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert run["exit_code"] == 3


@pytest.mark.asyncio
async def test_spawn_failure_is_terminal(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, opencode_path="/nonexistent/opencode-binary")
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert "could not start opencode" in (run["error"] or "")


@pytest.mark.asyncio
async def test_missing_model_marks_the_run_failed(env, monkeypatch):
    """Fail-fast model resolution still honors the terminal-state invariant:
    spawn raises, and the created row is already FAILED with the reason."""
    store, mod, tmp = env
    monkeypatch.delenv("JARVIS_RUN_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_BRAIN_MODEL", raising=False)
    ex = mod.RunExecutor(store, opencode_path=sys.executable,
                         opencode_argv_prefix=[_fake_opencode(tmp, None)])
    with pytest.raises(ValueError, match="no model configured"):
        await ex.spawn("do a thing", "proj", str(tmp), "api")
    failed = await asyncio.to_thread(store.list_runs, [store.RunStatus.FAILED])
    assert len(failed) == 1
    assert "no model configured" in (failed[0]["error"] or "")


@pytest.mark.asyncio
async def test_resume_continues_the_previous_opencode_session(
        env, tmp_path, monkeypatch):
    store, mod, tmp = env
    first = _executor(store, mod, tmp)
    prev_id = await first.spawn("first", "proj", str(tmp), "api")
    await first.wait_for(prev_id)

    marker = _marker_argv(tmp_path, monkeypatch, "argv2.json")
    second = _executor(store, mod, tmp)
    run_id = await second.spawn("second", "proj", str(tmp), "api",
                                resume_from=prev_id)
    await second.wait_for(run_id)
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert "--session" in argv
    assert argv[argv.index("--session") + 1] == RUN_OK_SESSION
    assert "--continue" in argv
    assert argv[-1] == "second"


@pytest.mark.asyncio
async def test_resume_without_a_session_starts_fresh(env, tmp_path,
                                                     monkeypatch):
    store, mod, tmp = env
    marker = _marker_argv(tmp_path, monkeypatch, "argv3.json")
    ex = _executor(store, mod, tmp)
    run_id = await ex.spawn("fresh", "proj", str(tmp), "api",
                            resume_from="no-such-run")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert "--session" not in argv and "--continue" not in argv


@pytest.mark.asyncio
async def test_tool_events_are_stored_verbatim(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(
        store, opencode_path=sys.executable,
        opencode_argv_prefix=[_fake_opencode(tmp, RUN_BASH)])
    run_id = await ex.spawn("run it", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED
    assert run["num_turns"] == 2
    events = store.get_events(run_id, limit=100)
    assert len(events) == 7
    kinds = [e["kind"] for e in events]
    assert kinds.count("step_finish") == 2 and "tool_use" in kinds
    assert "hello-tool-test" in (run["result_text"] or "")
