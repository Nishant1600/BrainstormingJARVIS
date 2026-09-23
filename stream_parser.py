"""Pure parsing of `claude -p --output-format stream-json` output.

No I/O. Everything here is driven by recorded fixtures in tests, which is
what lets the executor be tested without spawning a process.

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


def extract_init_metadata(event: dict) -> dict:
    return {
        "model": event.get("model") or "",
        "cwd": event.get("cwd") or "",
    }


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


def extract_result_metrics(event: dict) -> dict:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    result = event.get("result")
    return {
        "cost_usd": _as_float(event.get("total_cost_usd"), 0.0),
        "input_tokens": _as_int(usage.get("input_tokens"), 0),
        "output_tokens": _as_int(usage.get("output_tokens"), 0),
        "cache_read_tokens": _as_int(usage.get("cache_read_input_tokens"), 0),
        "cache_creation_tokens": _as_int(usage.get("cache_creation_input_tokens"), 0),
        "num_turns": _as_int(event.get("num_turns"), 0),
        "result_text": result if isinstance(result, str) else "",
        "is_error": bool(event.get("is_error")),
    }


def extract_assistant_usage(event: dict) -> dict:
    """Per-turn token usage from one `assistant` event.

    The CLI reports usage on every assistant turn but the dollar cost only
    once, in the terminal `result` event — so the executor accumulates these
    to show tokens climbing live, and never estimates a price from them.

    Defensive to the same degree as the other extractors: a non-dict
    `message`, a non-dict `usage`, missing keys and non-numeric values all
    yield zeros. A raise here would kill a live run.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        message = {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": _as_int(usage.get("input_tokens"), 0),
        "output_tokens": _as_int(usage.get("output_tokens"), 0),
        "cache_read_tokens": _as_int(usage.get("cache_read_input_tokens"), 0),
        "cache_creation_tokens": _as_int(
            usage.get("cache_creation_input_tokens"), 0),
    }


def summarize_assistant(event: dict) -> str:
    """One line describing what the agent is doing, for the live feed."""
    message = event.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if not isinstance(content, list):
        content = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = " ".join((block.get("text") or "").split())
            if text:
                return text[:_SUMMARY_MAX]
        elif block.get("type") == "tool_use":
            name = block.get("name") or "tool"
            target = (block.get("input") or {}).get("file_path") \
                or (block.get("input") or {}).get("command") \
                or (block.get("input") or {}).get("pattern") or ""
            target = " ".join(str(target).split())
            return (f"{name}: {target}".strip().rstrip(":"))[:_SUMMARY_MAX]
    return ""


# --- Did the run actually do anything, or did it stop to ask? -------------
#
# A spawned run is one-shot and non-interactive: nobody is on the other end.
# A run that ends its turn with a question has therefore stalled — it waits
# for an answer that can never arrive — and the pipeline's exit-0 reading of
# that is "succeeded", which is how "the site's ready, sir" was said about an
# empty directory.
#
# The signal is deliberately conservative in ONE direction: calling a genuine
# success a stall would be its own bug, so a run is only ever downgraded on
# POSITIVE evidence that it changed nothing.

# Tools that read, look, or think. Anything NOT in this set — including every
# tool a future CLI adds, and every MCP tool — counts as the run having done
# something, because the uncertain direction has to be "it worked".
READ_ONLY_TOOLS = frozenset({
    "read", "glob", "grep", "ls", "todowrite", "todoread",
    "websearch", "webfetch", "skill", "notebookread",
    "listmcpresources", "readmcpresource", "exitplanmode",
    "askuserquestion", "bashoutput", "killshell", "slashcommand",
})

OK = "ok"                  # it worked
STALLED = "stalled"        # it ended by asking a question, having changed nothing
NO_CHANGES = "no_changes"  # it ended cleanly, but nothing shows it changed anything

# Trailing decoration a model puts after the question mark.
_TRAILING = " \t\r\n*_`\"')】]>"


def assistant_parts(event: dict) -> tuple[str, list[str]]:
    """(all text in this assistant turn, the tool names it invoked)."""
    message = event.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if not isinstance(content, list):
        content = []
    texts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tools.append(str(block.get("name") or ""))
    return "\n".join(texts), tools


def ends_with_question(text: str) -> bool:
    """True when the last thing said was a question.

    Only the END counts. "What is X? It is Y." is an answer, not a request
    for one, and flagging that would turn a genuine success into a false
    alarm.
    """
    return (text or "").rstrip(_TRAILING).endswith("?")


def changed_anything(events: list[dict]) -> bool:
    """True if any assistant turn used a tool that is not purely read-only."""
    for event in events:
        if not isinstance(event, dict) or event_kind(event) != "assistant":
            continue
        _text, tools = assistant_parts(event)
        for name in tools:
            if name.lower() not in READ_ONLY_TOOLS:
                return True
    return False


def assess_outcome(events: list[dict], result_text: str = "") -> str:
    """OK, STALLED or NO_CHANGES for a run that exited zero.

    `events` must be the WHOLE stream: a partial view could miss the Write
    that proves work happened. A caller that cannot supply all of it passes
    nothing and takes OK — see server's `_run_outcome`.
    """
    assistant_events = [e for e in events
                        if isinstance(e, dict) and event_kind(e) == "assistant"]
    if not assistant_events:
        # No evidence either way. Never downgrade on an absence of data.
        return OK
    if changed_anything(assistant_events):
        return OK

    final = (result_text or "").strip()
    if not final:
        for event in reversed(assistant_events):
            text, _tools = assistant_parts(event)
            if text.strip():
                final = text.strip()
                break
    if final and ends_with_question(final):
        return STALLED
    return NO_CHANGES


# --- capping what goes into run_events.payload ----------------------------
#
# The executor stored every stream-json line verbatim, and
# claude_env.STREAM_LINE_LIMIT is 64 MiB — so a single line could be several
# MiB, permanently, in jarvis.db, and a chatty build emits thousands of
# events.
#
# 256 KiB is the threshold. It is far above anything a person would ever
# read: an entire long assistant message, or a large diff, is stored
# verbatim and the cap never fires. It is far below the point where a
# permanent database grows by gigabytes from one run.
#
# What gets shrunk is the long STRINGS inside the event, not the line. The
# stored payload is re-parsed downstream — by the dashboard, and by
# `assess_outcome`, which reads tool names out of assistant turns to decide
# whether a run actually did anything — so a payload that no longer parses
# would silently drop that evidence, and `changed_anything` going False
# turns a real success into a "no changes" alarm. Shrinking strings keeps
# every key, every type, and every tool name.
#
# 8 KiB per string is more than the dashboard renders in one block (it
# collapses long tool output anyway) and enough to recognise what a result
# was.
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
