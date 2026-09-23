# tests/test_opencode_parser.py
"""opencode_parser, driven by live-captured `opencode run --format json` output.

Fixtures in tests/fixtures/opencode/ were recorded against opencode 1.18.32:
run_ok.jsonl (3 events: step_start, text "OK", step_finish), run_err.jsonl
(1 error event from an unknown --model), run_bash.jsonl (7 events across
two steps, including a bash tool_use). If a future opencode changes the
vocabulary, re-capture and these tests say exactly what moved.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import opencode_parser

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


def _lines(name):
    return [opencode_parser.parse_line(l)
            for l in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_parse_line_returns_dict():
    assert opencode_parser.parse_line('{"type":"text"}') == {"type": "text"}


def test_parse_line_ignores_blank():
    assert opencode_parser.parse_line("   ") is None


def test_parse_line_ignores_non_json():
    assert opencode_parser.parse_line("not json at all") is None


def test_event_kind_reads_top_level_type():
    assert opencode_parser.event_kind({"type": "step_finish"}) == "step_finish"


def test_event_kind_defaults_to_unknown():
    assert opencode_parser.event_kind({}) == "unknown"


def test_ok_fixture_parses_completely():
    events = _lines("run_ok.jsonl")
    assert len(events) == 3
    assert all(e is not None for e in events)
    assert [opencode_parser.event_kind(e) for e in events] == [
        "step_start", "text", "step_finish"]


def test_bash_fixture_parses_completely():
    events = _lines("run_bash.jsonl")
    assert len(events) == 7
    assert all(e is not None for e in events)


def test_init_metadata_is_empty_by_design():
    """The stream carries no model or cwd (see module docstring) — the
    executor records the model from the spawn argv instead."""
    for e in _lines("run_ok.jsonl"):
        meta = opencode_parser.extract_init_metadata(e)
        assert meta == {"model": "", "cwd": ""}


def test_extract_usage_from_fixture_step_finish():
    finish = [e for e in _lines("run_ok.jsonl")
              if e.get("type") == "step_finish"][0]
    usage = opencode_parser.extract_assistant_usage(finish)
    assert usage == {"input_tokens": 11059, "output_tokens": 11,
                     "cache_read_tokens": 113, "cache_creation_tokens": 0}


def test_extract_usage_is_zero_off_step_finish():
    text = [e for e in _lines("run_ok.jsonl") if e.get("type") == "text"][0]
    assert opencode_parser.extract_assistant_usage(text) == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0}


def test_extract_usage_handles_non_dict_tokens():
    assert opencode_parser.extract_assistant_usage(
        {"type": "step_finish", "part": {"tokens": "not-a-dict"}}) == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0}


def test_extract_result_metrics_from_step_finish():
    finish = [e for e in _lines("run_ok.jsonl")
              if e.get("type") == "step_finish"][0]
    m = opencode_parser.extract_result_metrics(finish)
    assert m["cost_usd"] == 0.0
    assert m["input_tokens"] == 11059
    assert m["output_tokens"] == 11
    assert m["cache_read_tokens"] == 113
    assert m["cache_creation_tokens"] == 0
    assert m["num_turns"] == 0  # counted upstream, see module docstring
    assert m["result_text"] == ""
    assert m["is_error"] is False


def test_extract_result_metrics_flags_error_event():
    (err,) = _lines("run_err.jsonl")
    assert opencode_parser.event_kind(err) == "error"
    m = opencode_parser.extract_result_metrics(err)
    assert m["is_error"] is True
    assert m["result_text"] == (
        "Unexpected server error. Check server logs for details.")


def test_extract_result_metrics_handles_missing_part():
    m = opencode_parser.extract_result_metrics({"type": "step_finish"})
    assert m["cost_usd"] == 0.0
    assert m["input_tokens"] == 0
    assert m["is_error"] is False


def test_extract_result_metrics_preserves_exact_float_cost():
    m = opencode_parser.extract_result_metrics(
        {"type": "step_finish", "part": {"cost": 0.1461015,
                                         "tokens": {"input": 2}}})
    assert m["cost_usd"] == 0.1461015
    assert m["input_tokens"] == 2


def test_text_of_reads_text_events():
    (text,) = [e for e in _lines("run_ok.jsonl") if e.get("type") == "text"]
    assert opencode_parser.text_of(text) == "OK"


def test_text_of_is_empty_off_text_events():
    (start,) = [e for e in _lines("run_ok.jsonl")
                if e.get("type") == "step_start"]
    assert opencode_parser.text_of(start) == ""
    assert opencode_parser.text_of(
        {"type": "text", "part": {"text": ["not", "a", "string"]}}) == ""


def test_tool_of_names_bash_and_its_command():
    (use,) = [e for e in _lines("run_bash.jsonl")
              if e.get("type") == "tool_use"]
    name, summary = opencode_parser.tool_of(use)
    assert name == "bash"
    assert summary == "echo hello-tool-test"


def test_tool_of_is_empty_off_tool_use():
    assert opencode_parser.tool_of({"type": "text"}) == ("", "")


def test_tool_of_single_lines_a_hostile_tool_name():
    """A wire-controlled name must not be able to write a second line into
    the live feed (see the exemption in tests/test_header_lines.py). It
    still counts as work — an unknown tool is never read-only."""
    event = {"type": "tool_use",
             "part": {"tool": "bash\n evil",
                      "state": {"input": {"command": "id"}}}}
    name, summary = opencode_parser.tool_of(event)
    assert "\n" not in name and summary == "id"
    assert opencode_parser.changed_anything([event]) is True
    assert "\n" not in opencode_parser.summarize_event(event)


def test_summarize_event_extracts_text():
    (text,) = [e for e in _lines("run_ok.jsonl") if e.get("type") == "text"]
    assert opencode_parser.summarize_event(text) == "OK"


def test_summarize_event_names_tool_use():
    (use,) = [e for e in _lines("run_bash.jsonl")
              if e.get("type") == "tool_use"]
    assert opencode_parser.summarize_event(use) == "bash: echo hello-tool-test"


def test_summarize_event_truncates():
    assert len(opencode_parser.summarize_event(
        {"type": "text", "part": {"text": "x" * 500}})) <= 160


def test_summarize_event_is_quiet_for_frames():
    (start,) = [e for e in _lines("run_ok.jsonl")
                if e.get("type") == "step_start"]
    assert opencode_parser.summarize_event(start) == ""


def test_changed_anything_sees_bash():
    assert opencode_parser.changed_anything(_lines("run_bash.jsonl")) is True


def test_changed_anything_ignores_text_only_run():
    assert opencode_parser.changed_anything(_lines("run_ok.jsonl")) is False


def test_read_only_tools_do_not_count_as_changes():
    events = [{"type": "tool_use",
               "part": {"tool": "read",
                        "state": {"input": {"file_path": "/a/b.ts"}}}}]
    assert opencode_parser.changed_anything(events) is False


def test_assess_outcome_ok_when_tools_ran():
    assert opencode_parser.assess_outcome(
        _lines("run_bash.jsonl")) == opencode_parser.OK


def test_assess_outcome_no_changes_for_text_only_run():
    assert opencode_parser.assess_outcome(
        _lines("run_ok.jsonl")) == opencode_parser.NO_CHANGES


def test_assess_outcome_stalled_on_a_question():
    events = [{"type": "text", "part": {"text": "Dark theme or light?"}}]
    assert opencode_parser.assess_outcome(events) == opencode_parser.STALLED


def test_assess_outcome_ok_on_no_evidence():
    assert opencode_parser.assess_outcome([]) == opencode_parser.OK
    assert opencode_parser.assess_outcome(
        [{"type": "step_start", "part": {}}]) == opencode_parser.OK


def test_outcome_values_match_stream_parser():
    """Server code compares outcomes across runtimes — the strings agree."""
    import stream_parser
    assert opencode_parser.OK == stream_parser.OK
    assert opencode_parser.STALLED == stream_parser.STALLED
    assert opencode_parser.NO_CHANGES == stream_parser.NO_CHANGES


# --- the per-event payload cap (same contract as stream_parser) ---

def test_a_small_line_is_returned_untouched():
    line = json.dumps({"type": "text", "part": {"text": "OK"}})
    assert opencode_parser.cap_payload(line, json.loads(line)) == line


def test_an_enormous_tool_result_is_shrunk_but_still_parses():
    big = "x" * (2 * 1024 * 1024)
    event = {"type": "tool_use",
             "part": {"tool": "bash", "callID": "call_1",
                      "state": {"status": "completed",
                                "input": {"command": "cat big"},
                                "output": big}}}
    line = json.dumps(event)
    capped = opencode_parser.cap_payload(line, event)

    assert len(capped) < len(line) / 10
    parsed = json.loads(capped)
    assert parsed["type"] == "tool_use"
    assert parsed["part"]["callID"] == "call_1"
    assert parsed["part"]["state"]["output"].startswith("xxxx")
    assert "truncated" in parsed["part"]["state"]["output"]


def test_tool_names_survive_the_cap():
    """`assess_outcome` reads these to decide whether a run changed anything.
    Losing them turns a real success into a NO_CHANGES alarm."""
    event = {"type": "tool_use",
             "part": {"tool": "bash", "callID": "call_1",
                      "state": {"status": "completed",
                                "input": {"command": "x" * (1024 * 1024)}}}}
    capped = opencode_parser.cap_payload(json.dumps(event), event)
    reparsed = opencode_parser.parse_line(capped)
    assert reparsed is not None
    assert opencode_parser.changed_anything([reparsed]) is True
    assert opencode_parser.collect_tools([reparsed]) == ["bash"]


def test_a_line_that_is_long_from_sheer_count_still_gets_capped():
    event = {"type": "text", "part": {"n": [{"i": i} for i in range(200000)]}}
    line = json.dumps(event)
    assert len(line) > opencode_parser.PAYLOAD_MAX_CHARS
    capped = opencode_parser.cap_payload(line, event)
    assert len(capped) <= opencode_parser.PAYLOAD_MAX_CHARS
    parsed = json.loads(capped)
    assert parsed["type"] == "text"
    assert parsed["jarvis_truncated"] is True
    assert parsed["jarvis_original_chars"] == len(line)


def test_the_cap_is_documented_and_sane():
    assert 64 * 1024 <= opencode_parser.PAYLOAD_MAX_CHARS <= 1024 * 1024
    assert opencode_parser.PAYLOAD_STRING_MAX < opencode_parser.PAYLOAD_MAX_CHARS
