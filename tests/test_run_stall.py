"""A run that exited zero having built nothing is NOT a success.

The failure, verbatim from a live session:

    User:   is it still building
    JARVIS: Finished about a minute ago, sir — the site's ready in the
            project folder.
    User:   there's no way though I mean that happened very fast
    JARVIS: That's what run_status reports, sir — done and successful.

`~/Projects/tony-starks-website/` held `.git` and `README.md`. The run had
loaded an interactive planning skill, asked one clarifying question, and
ended its turn — `stop_reason: end_turn`, `is_error: false`, exit 0.

The property under test is two-sided, and the second side matters as much as
the first: a run that ended by asking is reported honestly, AND a run that
genuinely worked is still reported as having worked. Downgrading a real
success would be its own bug, so the detector only ever acts on positive
evidence that nothing changed.
"""

import importlib
import json

import pytest

import stream_parser


# --- the pure decision ----------------------------------------------------

def _assistant(text="", tools=()):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name in tools:
        content.append({"type": "tool_use", "name": name, "input": {}})
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": content}}


def test_a_question_after_nothing_but_reading_is_a_stall():
    events = [
        _assistant(tools=["Skill"]),
        _assistant(tools=["Read", "Glob"]),
        _assistant("One quick question: for the bio section, do you want an "
                   "in-universe first-person voice, or a neutral third-person "
                   "bio style?"),
    ]
    assert stream_parser.assess_outcome(events) == stream_parser.STALLED


def test_a_run_that_wrote_files_is_a_success_even_if_it_ends_by_asking():
    """"Done — want me to deploy it?" is a finished piece of work."""
    events = [
        _assistant(tools=["Write"]),
        _assistant(tools=["Edit"]),
        _assistant("Built the page. Want me to deploy it?"),
    ]
    assert stream_parser.assess_outcome(events) == stream_parser.OK


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "Bash",
                                  "NotebookEdit", "Task",
                                  "mcp__something__do_a_thing",
                                  "SomeToolShippedNextMonth"])
def test_anything_that_is_not_plainly_read_only_counts_as_work(tool):
    """The uncertain direction has to be "it worked" — a tool nobody has
    heard of must never be the reason a real success is downgraded."""
    events = [_assistant(tools=[tool]), _assistant("Shall I carry on?")]
    assert stream_parser.assess_outcome(events) == stream_parser.OK


def test_a_question_mark_in_the_middle_is_not_a_question_at_the_end():
    events = [_assistant("What was slow? The N+1 query in the exporter. "
                         "I've left it alone as you asked.")]
    assert stream_parser.assess_outcome(events) == stream_parser.NO_CHANGES


def test_trailing_markdown_does_not_hide_the_question():
    events = [_assistant("Which should I use?**")]
    assert stream_parser.assess_outcome(events) == stream_parser.STALLED


def test_the_result_text_is_the_final_word_when_it_is_there():
    events = [_assistant("thinking out loud")]
    assert stream_parser.assess_outcome(
        events, "So — dark theme or light?") == stream_parser.STALLED


def test_no_assistant_events_at_all_is_never_downgraded():
    """No evidence is not evidence of failure."""
    assert stream_parser.assess_outcome([]) == stream_parser.OK
    assert stream_parser.assess_outcome(
        [{"type": "system"}], "did you want blue?") == stream_parser.OK


def test_malformed_events_do_not_raise():
    for junk in ([None], [{"type": "assistant", "message": "not a dict"}],
                 [{"type": "assistant", "message": {"content": "nope"}}],
                 [{"type": "assistant", "message": {"content": [None, 3]}}]):
        assert stream_parser.assess_outcome(junk) in (
            stream_parser.OK, stream_parser.NO_CHANGES)


# --- what the user actually hears -----------------------------------------

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


def _finished_run(store, events, result_text="", project="tony-starks-website"):
    run_id = store.create_run("build a site", project, "/tmp/x", "voice")
    for i, event in enumerate(events, start=1):
        store.append_event(run_id, i, event.get("type", "assistant"),
                           json.dumps(event))
    store.update_run(run_id, status=store.RunStatus.SUCCEEDED,
                     ended_at=1.0, result_text=result_text)
    return store.get_run(run_id)


def test_a_stalled_run_is_not_reported_as_a_plain_success(wired):
    server, store = wired
    run = _finished_run(store, [
        _assistant(tools=["Skill"]),
        _assistant("Do you want an in-universe voice, or a neutral one?"),
    ], result_text="Do you want an in-universe voice, or a neutral one?")

    said = server._describe_run(run)

    assert "and it worked" not in said
    assert "stopped to ask a question" in said
    assert "nothing was built" in said
    assert "tony-starks-website" in said


def test_a_genuine_success_is_still_a_success(wired):
    server, store = wired
    run = _finished_run(store, [
        _assistant(tools=["Write"]),
        _assistant("Built index.html and styles.css."),
    ], result_text="Built index.html and styles.css.")

    assert "and it worked" in server._describe_run(run)


def test_a_run_with_no_recorded_events_is_still_a_success(wired):
    """The pipeline is the only source of the evidence. Without it, the
    honest answer is the one the store already holds."""
    server, store = wired
    run = _finished_run(store, [])
    assert "and it worked" in server._describe_run(run)


def test_a_failed_run_still_reports_failure(wired):
    server, store = wired
    run = _finished_run(store, [_assistant("Shall I?")])
    store.update_run(run["id"], status=store.RunStatus.FAILED,
                     error="exit code 1")
    said = server._describe_run(store.get_run(run["id"]))
    assert "failed" in said


def test_a_run_that_changed_nothing_and_asked_nothing_says_so(wired):
    server, store = wired
    run = _finished_run(store, [_assistant(tools=["Read"]),
                                _assistant("Had a look around.")])
    said = server._describe_run(run)
    assert "can't see that it changed anything" in said
    assert "and it worked" not in said


def test_the_question_itself_is_relayed_wrapped_as_untrusted(wired):
    """So the user can answer it — but it is another process's text, and it
    is labelled as such like everything else that comes out of one."""
    server, store = wired
    question = "In-universe first-person, or a neutral third-person bio?"
    run = _finished_run(store, [_assistant(question)], result_text=question)

    said = server._describe_run(run, with_reason=True)

    assert "<session-output" in said and "</session-output>" in said
    assert question in said


def test_run_status_reports_the_stall(wired):
    server, store = wired
    _finished_run(store, [_assistant("Which theme, sir?")],
                  result_text="Which theme, sir?")
    out = server.tool_run_status({"run": "tony-starks-website"})
    assert "stopped to ask a question" in out


def test_a_huge_run_is_never_downgraded(wired, monkeypatch):
    """Reading a partial stream could miss the Write that proves work
    happened, so past the cap the answer is the one that cannot be wrong."""
    server, store = wired
    monkeypatch.setattr(server, "_OUTCOME_EVENT_CAP", 2)
    run = _finished_run(store, [_assistant(tools=["Read"]),
                                _assistant(tools=["Read"]),
                                _assistant("Which one?")])
    assert "and it worked" in server._describe_run(run)


def test_an_unreadable_event_stream_fails_open(wired, monkeypatch):
    server, store = wired
    run = _finished_run(store, [_assistant("Which one?")])

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server.run_store, "count_events", boom)
    assert "and it worked" in server._describe_run(run)


# --- the announcement -----------------------------------------------------

class _Speech:
    def __init__(self):
        self.said = []

    async def say(self, text, priority=None, **k):
        self.said.append((text, priority))


@pytest.mark.asyncio
async def test_a_stalled_run_is_announced_honestly_not_batched(wired,
                                                              monkeypatch):
    server, store = wired
    speech = _Speech()
    monkeypatch.setattr(server, "speech", speech)
    run = _finished_run(store, [_assistant("Which theme?")],
                        result_text="Which theme?")

    server._on_run_event({"type": "run_finished", "run": run})
    for _ in range(3):
        await __import__("asyncio").sleep(0)

    assert server._pending_run_completions == [], "never 'the work is done'"
    assert speech.said, "the user must be told"
    assert "stopped to ask a question" in speech.said[0][0]


@pytest.mark.asyncio
async def test_a_real_success_is_still_batched_as_done(wired, monkeypatch):
    server, store = wired
    monkeypatch.setattr(server, "speech", _Speech())
    run = _finished_run(store, [_assistant(tools=["Write"])])

    server._on_run_event({"type": "run_finished", "run": run})

    assert server._pending_run_completions == ["tony-starks-website"]


# --- the opencode vocabulary is judged by its own parser -------------------

def _text(text):
    return {"type": "text", "sessionID": "ses_x",
            "part": {"type": "text", "text": text}}


def _tool_use(tool, summary=""):
    state = {"status": "completed", "input": {"command": summary}}
    return {"type": "tool_use", "sessionID": "ses_x",
            "part": {"type": "tool", "tool": tool, "callID": "call_1",
                     "state": state}}


def _step_finish():
    return {"type": "step_finish", "sessionID": "ses_x",
            "part": {"type": "step-finish", "reason": "stop",
                     "tokens": {"input": 10, "output": 2,
                                "cache": {"read": 0, "write": 0}},
                     "cost": 0}}


def test_an_opencode_stall_is_reported_honestly(wired):
    server, store = wired
    run = _finished_run(store, [_text("Dark theme or light?")],
                        result_text="Dark theme or light?")
    said = server._describe_run(run)
    assert "stopped to ask a question" in said
    assert "and it worked" not in said


def test_an_opencode_success_with_tools_is_still_a_success(wired):
    server, store = wired
    run = _finished_run(store, [_tool_use("bash", "echo hi"),
                                _text("It printed hi.")],
                        result_text="It printed hi.")
    assert "and it worked" in server._describe_run(run)


def test_an_opencode_text_only_run_reports_no_changes(wired):
    server, store = wired
    run = _finished_run(store, [_text("All done.")],
                        result_text="All done.")
    said = server._describe_run(run)
    assert "can't see that it changed anything" in said
    assert "and it worked" not in said


def test_a_mixed_vocabulary_stream_fails_open(wired):
    """A stream matching neither parser's vocabulary is OK: no evidence
    either way is never a downgrade, even when the events parse."""
    server, store = wired
    run = _finished_run(store, [{"type": "mystery", "part": {}}])
    assert "and it worked" in server._describe_run(run)


# --- the runtime switch ----------------------------------------------------

def test_the_executor_defaults_to_the_claude_runtime(wired, monkeypatch):
    monkeypatch.delenv("JARVIS_RUNTIME", raising=False)
    import server as fresh
    importlib.reload(fresh)
    assert fresh.run_executor_instance._runtime == "claude"
    assert fresh.run_executor_instance._is_opencode is False


def test_the_executor_follows_jarvis_runtime_and_opencode_path(
        wired, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME", "opencode")
    monkeypatch.setenv("JARVIS_OPENCODE_PATH", str(tmp_path / "opencode"))
    import server as fresh
    importlib.reload(fresh)
    assert fresh.run_executor_instance._runtime == "opencode"
    assert fresh.run_executor_instance._is_opencode is True
    assert fresh.run_executor_instance._opencode == str(tmp_path / "opencode")


def test_an_unknown_runtime_falls_back_to_claude(wired, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME", "wat")
    import server as fresh
    importlib.reload(fresh)
    assert fresh.run_executor_instance._runtime == "claude"
