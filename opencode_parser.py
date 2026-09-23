"""Pure parsing of `opencode run --format json` output.

No I/O. Companion to `stream_parser.py` (Claude Code's stream-json) with the
same public shape, so the run-executor retarget stays a small diff.
Everything here is driven by recorded fixtures in
`tests/fixtures/opencode/` — captured live against opencode 1.18.32 — which
is what lets the executor be tested without spending provider quota.

Measured event vocabulary (top-level `type`):

  step_start   a model step began; carries no text and no usage.
  text         one assistant text part: `part.text`.
  tool_use     one tool call: `part.tool`, `part.callID`, and
               `part.state{status, input, output, ...}`.
  step_finish  a step ended: `part.reason` (`stop`, `tool-calls`, ...),
               `part.tokens{input, output, reasoning, cache{read, write}}`,
               `part.cost`.
  error        the run failed: `error{name, data{message, ref}}`.

Three differences from the Claude Code stream that callers must know:

* There is NO `init` event. The stream carries no model and no cwd — the
  model comes from the argv JARVIS spawned (`--model`), never from here.
  `extract_init_metadata` therefore returns empty strings, and the
  executor keeps its "update only when truthy" guard.
* There is NO terminal `result` event. The process exiting 0 IS the result.
  Per-step usage arrives on every `step_finish` — the analogue of the CLI's
  per-turn assistant usage — and accumulates into the same run columns.
  Dollars are never derived from steps; cost is recorded from the steps'
  own `cost` fields, never estimated.
* `num_turns` is not reported per event. The executor counts `step_finish`
  events for the run's step total; `extract_result_metrics` reports 0 for
  any single event. (Claude's `result.num_turns` was authoritative; here
  the count lives one layer up, where the whole stream is visible.)

The CLI's event vocabulary is not a stable contract, so unrecognized
events are preserved verbatim rather than interpreted.
"""

import json

_SUMMARY_MAX = 160


def parse_line(line: str) -> dict | None:
    """Parse one JSONL line. Returns None for blank or malformed lines."""
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def event_kind(event: dict) -> str:
    """The event's top-level `type`, stored verbatim."""
    return event.get("type") or "unknown"


def _part(event: dict) -> dict:
    """The event's `part` object, or {} — every content carrier lives there."""
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_init_metadata(event: dict) -> dict:
    """{"model": "", "cwd": ""}, always.

    The opencode stream carries neither — see the module docstring. The
    executor records the model from the spawn argv instead, and only when
    the value is truthy, so these empties are a safe no-op there.
    """
    return {
        "model": "",
        "cwd": "",
    }


def _tokens_of(event: dict) -> dict:
    """The `part.tokens` object, or {} — usage lives on `step_finish` only."""
    tokens = _part(event).get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def extract_assistant_usage(event: dict) -> dict:
    """Per-step token usage from one `step_finish` event.

    Same key names as `stream_parser.extract_assistant_usage`, so the
    executor's accumulation loop is unchanged: these add into the run's
    live columns, and dollars are never derived from them.
    `cache.write` is the creation side, mirroring `cache_creation_tokens`.
    Non-`step_finish` events carry no usage and yield zeros.

    Defensive to the same degree as the other extractors: a non-dict
    `part`, a non-dict `tokens` or `cache`, and missing or non-numeric
    values all yield zeros. A raise here would kill a live run.
    """
    tokens = _tokens_of(event)
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    return {
        "input_tokens": _as_int(tokens.get("input"), 0),
        "output_tokens": _as_int(tokens.get("output"), 0),
        "cache_read_tokens": _as_int(cache.get("read"), 0),
        "cache_creation_tokens": _as_int(cache.get("write"), 0),
    }


def extract_result_metrics(event: dict) -> dict:
    """Cost and error verdict for one terminal-shaped event.

    For `step_finish`: the step's own `cost` (usually 0 — summed upstream,
    never estimated) and its token counts; `num_turns` is 0 (counted
    upstream, see the module docstring); `is_error` is False.
    For `error`: zeros plus `is_error` True and the CLI's message as
    `result_text` — the analogue of Claude's `is_error` result, which the
    executor's `saw_error` latch exists to catch even on exit 0.
    Anything else: all defaults.
    """
    if event_kind(event) == "error":
        err = event.get("error")
        message = ""
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict):
                message = data.get("message") or ""
            message = message if isinstance(message, str) else ""
        return {
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "num_turns": 0,
            "result_text": message if isinstance(message, str) else "",
            "is_error": True,
        }
    usage = extract_assistant_usage(event)
    return {
        "cost_usd": _as_float(_part(event).get("cost"), 0.0),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_creation_tokens": usage["cache_creation_tokens"],
        "num_turns": 0,
        "result_text": "",
        "is_error": False,
    }


def text_of(event: dict) -> str:
    """The text carried by a `text` event, or "" for anything else."""
    if event_kind(event) != "text":
        return ""
    text = _part(event).get("text")
    return text if isinstance(text, str) else ""


def tool_of(event: dict) -> tuple[str, str]:
    """(tool name, one-line input summary) for a `tool_use` event.

    The summary names the most telling input field — command, file path,
    pattern, or URL — the same way `stream_parser.summarize_assistant`
    does for Claude tool_use blocks, so the live feed reads the same.
    Anything that is not a `tool_use` event yields ("", "").
    """
    if event_kind(event) != "tool_use":
        return "", ""
    part = _part(event)
    name = part.get("tool")
    name = name if isinstance(name, str) else ""
    # Single line, like the summary below: the name arrives off the wire
    # and a hostile one could carry a newline, which must not reach a
    # live-feed line (see summarize_event's exemption in
    # tests/test_header_lines.py). Matching for READ_ONLY_TOOLS happens in
    # changed_anything against this same value, so nothing silently stops
    # counting as work.
    name = " ".join(name.split())
    state = part.get("state")
    inputs = state.get("input") if isinstance(state, dict) else None
    summary = ""
    if isinstance(inputs, dict):
        for key in ("command", "file_path", "path", "pattern", "url",
                    "query", "prompt"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                summary = " ".join(value.split())
                break
    return name, summary


def summarize_event(event: dict) -> str:
    """One line describing the event, for the live feed."""
    kind = event_kind(event)
    if kind == "text":
        return " ".join(text_of(event).split())[:_SUMMARY_MAX]
    if kind == "tool_use":
        name, summary = tool_of(event)
        return (f"{name}: {summary}".strip().rstrip(":"))[:_SUMMARY_MAX] or name
    if kind == "error":
        err = event.get("error")
        message = ""
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                message = " ".join(data["message"].split())
        return (f"error: {message}".strip().rstrip(":"))[:_SUMMARY_MAX]
    return ""


def collect_text(events: list[dict]) -> str:
    """All assistant text in the stream, in order."""
    return "\n".join(text_of(e) for e in events
                      if isinstance(e, dict) and text_of(e))


def collect_tools(events: list[dict]) -> list[str]:
    """Every tool name invoked in the stream, in order."""
    out: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        name, _summary = tool_of(event)
        if name:
            out.append(name)
    return out


# --- Did the run actually do anything, or did it stop to ask? -------------
#
# Same question `stream_parser.assess_outcome` answers for Claude runs, with
# the same conservative direction: a genuine success is never downgraded
# without POSITIVE evidence, so a run is only ever flagged on proof it
# changed nothing. A spawned run is one-shot and non-interactive — a run
# that ends by asking a question waits for an answer that can never arrive.

# Tools that read, look, or plan. Anything NOT in this set — including every
# tool a future opencode adds, and every MCP tool — counts as the run having
# done something, because the uncertain direction has to be "it worked".
# `bash` is deliberately absent: running a command changes the world (or
# proves the run could), even when the command itself only reads.
READ_ONLY_TOOLS = frozenset({
    "read", "glob", "grep", "list",
    "webfetch", "websearch",
    "todowrite", "todoread",
})

# Outcome values are the same strings `stream_parser` uses ("ok",
# "stalled", "no_changes") on purpose: callers compare outcomes across
# runtimes, and string equality keeps those comparisons true no matter
# which module's constant is on either side.
OK = "ok"                  # it worked
STALLED = "stalled"        # it ended by asking a question, having changed nothing
NO_CHANGES = "no_changes"  # it ended cleanly, but nothing shows it changed anything

# Trailing decoration a model puts after the question mark. Same rule as
# `stream_parser.ends_with_question` — the twin must stay in lockstep; a
# question is only ever recognised at the END of what was said.
_TRAILING = " \t\r\n*_`\"')】]>"


def ends_with_question(text: str) -> bool:
    """True when the last thing said was a question. Only the END counts."""
    return (text or "").rstrip(_TRAILING).endswith("?")


def changed_anything(events: list[dict]) -> bool:
    """True if any `tool_use` event names a tool that is not purely read-only."""
    for name in collect_tools(events):
        if name.lower() not in READ_ONLY_TOOLS:
            return True
    return False


def assess_outcome(events: list[dict], result_text: str = "") -> str:
    """OK, STALLED or NO_CHANGES for a run whose process exited zero.

    `events` must be the WHOLE stream: a partial view could miss the `bash`
    call that proves work happened. A caller that cannot supply all of it
    passes nothing and takes OK.
    """
    ours = [e for e in events if isinstance(e, dict)]
    if not ours:
        # No evidence either way. Never downgrade on an absence of data.
        return OK
    if changed_anything(ours):
        return OK

    final = (result_text or "").strip()
    if not final:
        texts = [text_of(e) for e in reversed(ours)]
        final = next((t.strip() for t in texts if t.strip()), "")
    if final and ends_with_question(final):
        return STALLED
    if any(text_of(e).strip() for e in ours):
        return NO_CHANGES
    # No text and no tools at all (bare step_start/step_finish frames, or
    # unrecognised events only): no evidence either way, so OK — the same
    # "never downgrade on an absence of data" as the empty case above.
    return OK


# --- capping what goes into run_events.payload ----------------------------
#
# Same cap as `stream_parser.cap_payload`, for the same reason: the executor
# stores every `--format json` line verbatim, and a single line carrying a
# large tool result can be several MiB, permanently, in jarvis.db.
#
# What gets shrunk is the long STRINGS inside the event, not the line. The
# stored payload is re-parsed downstream — `assess_outcome` reads tool names
# out of `tool_use` events to decide whether a run actually did anything —
# so a payload that no longer parses would silently drop that evidence, and
# `changed_anything` going False turns a real success into a "no changes"
# alarm. Shrinking strings keeps every key, every type, and every tool name.
PAYLOAD_MAX_CHARS = 256 * 1024
PAYLOAD_STRING_MAX = 8 * 1024


def _shrink(value, budget: int):
    if isinstance(value, str):
        if len(value) <= budget:
            return value
        return f"{value[:budget]}… [truncated, {len(value)} chars]"
    if isinstance(value, dict):
        return {k: _shrink(v, budget) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v, budget) for v in value]
    return value


def cap_payload(line: str, event: dict) -> str:
    """The JSONL line to persist for `event`, bounded in size.

    Returns `line` unchanged below the cap — which is the overwhelmingly
    common case, so this costs nothing on a normal run.
    """
    if len(line) <= PAYLOAD_MAX_CHARS:
        return line
    try:
        shrunk = json.dumps(_shrink(event, PAYLOAD_STRING_MAX))
    except (TypeError, ValueError):
        shrunk = ""
    if shrunk and len(shrunk) <= PAYLOAD_MAX_CHARS:
        return shrunk
    # Long from sheer element count, not from any one string — there is
    # nothing left to shrink. Keep the type (every downstream reader keys off
    # it) and say plainly what happened.
    return json.dumps({
        "type": event_kind(event),
        "jarvis_truncated": True,
        "jarvis_original_chars": len(line),
        "jarvis_preview": line[:PAYLOAD_STRING_MAX],
    })
