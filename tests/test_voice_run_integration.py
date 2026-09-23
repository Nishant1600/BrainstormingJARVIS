"""What is left of the old voice dispatch path, after the chain itself went.

This file used to drive `_execute_prompt_project` -> `_await_and_report` ->
`_report_run_result` and the `_start_work_project` / `_work_project_send`
work-mode pair. That whole chain was superseded by the brain + RunExecutor +
TOOL_HANDLERS architecture and had no non-test caller left; it was the only
reason `server.py` imported the `anthropic` SDK, so it and the dependency are
gone (see `test_no_anthropic_sdk.py`, which pins the absence).

The behaviours those tests protected are all still covered, on the live paths
that replaced them:

- announcing a finished run by voice, and never calling a failed or timed-out
  run a success -> `test_run_announcements.py` (`test_a_failure_interrupts_
  and_says_it_failed`, `test_a_timeout_is_announced_as_running_out_of_time`)
  and `test_announcements.py`
- a run always reaching a terminal state -> `test_run_executor.py`
  (`test_store_failure_still_reaches_terminal_state`,
  `test_terminal_state_invariant_holds_with_explicit_model`, and the
  oversized-line cases)
- turn N resuming from turn N-1 -> `test_spawn_run_tool.py`, which pins
  `resume_from` on the live `spawn_run` tool

What remains here are the two helpers that outlived the chain and are reached
from elsewhere, plus the guard that the pre-RunExecutor dispatch registry
stays deleted.
"""
import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    return server, run_store, tmp_path


def test_dispatch_registry_is_gone(env):
    server, _, _ = env
    assert not hasattr(server, "dispatch_registry")


# ---------------------------------------------------------------------------
# format_runs_for_prompt — what JARVIS is working on, as prompt context
# ---------------------------------------------------------------------------

def test_format_runs_for_prompt_lists_active(env):
    server, store, _ = env
    run_id = store.create_run("build the thing", "alpha", "/tmp/alpha", "voice")
    store.update_run(run_id, status=store.RunStatus.RUNNING)
    text = server.format_runs_for_prompt()
    assert "alpha" in text
    assert "build the thing" in text


def test_format_runs_for_prompt_when_empty(env):
    server, _, _ = env
    assert "No active" in server.format_runs_for_prompt()


def test_format_runs_for_prompt_lists_recent_completed(env):
    server, store, _ = env
    run_id = store.create_run("ship it", "beta", "/tmp/beta", "voice")
    store.update_run(run_id, status=store.RunStatus.SUCCEEDED,
                     summary="Shipped the landing page")
    text = server.format_runs_for_prompt()
    assert "beta" in text


# ---------------------------------------------------------------------------
# The run-store reads that replaced the old dispatch registry's get_most_recent
# ---------------------------------------------------------------------------

def test_check_dispatch_reads_the_most_recently_created_run(env):
    """The get_most_recent() replacement used by the check_dispatch action."""
    _server, store, _ = env
    store.create_run("first", "alpha", "/tmp/alpha", "voice")
    time.sleep(0.01)
    second = store.create_run("second", "beta", "/tmp/beta", "voice")
    recent = (store.list_runs(limit=1) or [None])[0]
    assert recent is not None
    assert recent["id"] == second
    assert recent["project_name"] == "beta"


def test_check_dispatch_handles_an_empty_run_store(env):
    _server, store, _ = env
    assert (store.list_runs(limit=1) or [None])[0] is None
