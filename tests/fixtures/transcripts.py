"""Build Claude Code transcripts carrying token usage, as measured live.

Every field here was copied from a real transcript on 2026-09-03. Two shapes
matter:

  <root>/projects/<encoded cwd>/<sessionId>.jsonl
      the conversation itself. `isSidechain` is false on every line.

  <root>/projects/<encoded cwd>/<sessionId>/subagents/agent-<agentId>.jsonl
      one subagent dispatched by that conversation. Same `sessionId`,
      `isSidechain: true`, and its own `agentId`.

If the CLI changes shape, this is the file to re-measure.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# The usage block of a real assistant line, trimmed to the keys that are read.
# `iterations`, `server_tool_use`, `service_tier` and friends are present live
# and deliberately ignored — a future key must not change a total.
CLI_VERSION = "2.1.251"


def encode(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def assistant_line(*, session_id: str, cwd: str, when: float,
                   model: str = "claude-sonnet-5",
                   inp: int = 0, out: int = 0, cache_read: int = 0,
                   cache_creation: int = 0, sidechain: bool = False,
                   agent_id: str | None = None, text: str = "working") -> str:
    line = {
        "type": "assistant",
        "isSidechain": sidechain,
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": iso(when),
        "version": CLI_VERSION,
        "requestId": f"req_{int(when)}",
        "uuid": f"u{int(when * 1000)}",
        "message": {
            "role": "assistant",
            "id": f"msg_{int(when)}",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens_details": {"thinking_tokens": 0},
                "service_tier": "standard",
            },
        },
    }
    if agent_id:
        line["agentId"] = agent_id
    return json.dumps(line)


def noise_lines(session_id: str) -> list[str]:
    """Line types that carry no usage and must never raise or count."""
    return [
        json.dumps({"type": "ai-title", "aiTitle": "a title",
                    "sessionId": session_id}),
        json.dumps({"type": "mode", "mode": "default", "sessionId": session_id}),
        json.dumps({"type": "attachment", "isSidechain": False,
                    "sessionId": session_id,
                    "attachment": {"type": "deferred_tools_delta"}}),
        "",                       # blank
        "{not json at all",       # caught mid-write
        json.dumps([1, 2, 3]),    # a list, not an object
    ]


def write_transcript(root: Path, *, cwd: str, session_id: str,
                     turns: list[dict], noise: bool = True) -> Path:
    """One conversation transcript. `turns` are kwargs for `assistant_line`."""
    d = Path(root) / "projects" / encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    lines = []
    if noise:
        lines += noise_lines(session_id)
    for t in turns:
        lines.append(assistant_line(session_id=session_id, cwd=cwd, **t))
    p.write_text("\n".join(lines) + "\n")
    return p


def append_turns(path: Path, *, session_id: str, cwd: str,
                 turns: list[dict]) -> None:
    """Append to an existing transcript, exactly as the CLI does."""
    with open(path, "a") as fh:
        for t in turns:
            fh.write(assistant_line(session_id=session_id, cwd=cwd, **t) + "\n")


def write_agent_sidecar(root: Path, *, cwd: str, session_id: str,
                        agent_id: str, agent_type: str = "general-purpose",
                        description: str = "Research the thing",
                        parent_agent_id: str | None = None,
                        spawn_depth: int = 1) -> Path:
    """The `agent-<id>.meta.json` the CLI writes beside each subagent
    transcript.

    Measured live 2026-09-03: one of these sits next to EVERY subagent
    transcript — 209 `.meta.json` beside 209 `.jsonl` in one folder — and it
    is the only place the agent's TYPE, its one-line description and its
    spawn depth are written down.

    THE NAME IS `.meta.json`, NOT `.json`. This fixture said `.json` first
    and the code it tested agreed with it, so the test passed green against
    a file the CLI has never written. Re-measure here if the shape changes;
    do not adjust it to match the code.
    """
    d = Path(root) / "projects" / encode(cwd) / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.meta.json"
    body = {"agentType": agent_type, "description": description,
            "toolUseId": f"toolu_{agent_id}", "spawnDepth": spawn_depth}
    if parent_agent_id:
        body["parentAgentId"] = parent_agent_id
    p.write_text(json.dumps(body))
    return p


def write_agent_transcript(root: Path, *, cwd: str, session_id: str,
                           agent_id: str, turns: list[dict],
                           prompt: str = "go and look") -> Path:
    """One subagent transcript, in the `<sessionId>/subagents/` folder."""
    d = Path(root) / "projects" / encode(cwd) / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    lines = [json.dumps({
        "type": "user", "isSidechain": True, "agentId": agent_id,
        "sessionId": session_id, "cwd": cwd, "parentUuid": None,
        "version": CLI_VERSION, "slug": "vivid-sleeping-alpaca",
        "timestamp": iso(turns[0]["when"] - 1 if turns else 0),
        "message": {"role": "user", "content": prompt},
    })]
    for t in turns:
        lines.append(assistant_line(session_id=session_id, cwd=cwd,
                                    sidechain=True, agent_id=agent_id, **t))
    p.write_text("\n".join(lines) + "\n")
    return p
