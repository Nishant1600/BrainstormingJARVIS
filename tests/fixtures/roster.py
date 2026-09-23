"""Build fixture roster roots and transcripts matching the shapes measured
from the live machine on 2026-09-03.

Every field name and every observed value here was copied from a real file;
if the CLI changes shape, this is the file to re-measure.
"""

import json
import re
import time
from pathlib import Path

NOW_MS = 1788404571964            # the timestamp on the real roster entry


def write_roster(root: Path, *, pid: int, session_id: str, cwd: str, name: str,
                 status: str | None = "idle", waiting_for: str | None = None,
                 socket: bool = True, kind: str = "interactive",
                 entrypoint: str = "cli", started_at: int = NOW_MS,
                 status_updated_at: int | None = NOW_MS,
                 extra: dict | None = None) -> Path:
    """Write one `sessions/<pid>.json` exactly as the CLI writes it."""
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    entry = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": started_at,
        "procStart": time.ctime(started_at / 1000),
        "version": "2.1.251",
        "peerProtocol": 1,
        "peerFeatures": ["notify_idle", "reply_across_default_dirs"],
        "kind": kind,
        "entrypoint": entrypoint,
        "pidDomain": "darwin",
        "name": name,
        "nameSource": "derived",
        "nameSince": started_at,
    }
    if socket:
        entry["messagingSocketPath"] = f"/tmp/cc-socks/{pid}.sock"
    if status is not None:
        entry["status"] = status
        entry["updatedAt"] = status_updated_at
    if status_updated_at is not None:
        entry["statusUpdatedAt"] = status_updated_at
    if waiting_for is not None:
        entry["waitingFor"] = waiting_for
    if extra:
        entry.update(extra)
    p = d / f"{pid}.json"
    p.write_text(json.dumps(entry))
    return p


def encode(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def write_transcript(root: Path, *, cwd: str, session_id: str,
                     title: str | None = None, last_prompt: str | None = None,
                     assistant_texts: list[str] = (),
                     tools: list[str] = (), padding_kb: int = 0,
                     sidechain_text: str | None = None) -> Path:
    """Write a `.jsonl` transcript using the 19 line types the CLI really emits.

    `padding_kb` prepends filler lines so tests can prove the 64 KB tail still
    finds what it needs in a file far larger than the tail.
    """
    d = root / "projects" / encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    lines: list[str] = []

    if padding_kb:
        filler = {"type": "user", "isSidechain": False, "sessionId": session_id,
                  "message": {"role": "user",
                              "content": [{"type": "tool_result",
                                           "tool_use_id": "old",
                                           "content": "x" * 900}]}}
        for _ in range((padding_kb * 1024) // 950 + 1):
            lines.append(json.dumps(filler))

    if sidechain_text:
        lines.append(json.dumps({
            "type": "assistant", "isSidechain": True, "sessionId": session_id,
            "timestamp": "2026-09-03T02:00:00.000Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": sidechain_text}]}}))

    for i, name in enumerate(tools):
        lines.append(json.dumps({
            "type": "assistant", "isSidechain": False, "sessionId": session_id,
            "timestamp": f"2026-09-03T02:10:{i:02d}.000Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "content": [{"type": "tool_use", "id": f"t{i}",
                                     "name": name, "input": {}}]}}))

    for i, text in enumerate(assistant_texts):
        lines.append(json.dumps({
            "type": "assistant", "isSidechain": False, "sessionId": session_id,
            "timestamp": f"2026-09-03T02:20:{i:02d}.000Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": text}]}}))

    # These two are rewritten by the CLI on every turn, which is why a tail finds them.
    if title is not None:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title,
                                 "sessionId": session_id}))
    if last_prompt is not None:
        lines.append(json.dumps({"type": "last-prompt", "lastPrompt": last_prompt,
                                 "leafUuid": "leaf", "sessionId": session_id}))
    # Line types the parser must ignore without error.
    lines.append(json.dumps({"type": "mode", "mode": "default",
                             "sessionId": session_id}))
    lines.append(json.dumps({"type": "attachment", "isSidechain": False,
                             "attachment": {"type": "total_tokens_reminder",
                                            "text": "<total_tokens>1</total_tokens>"},
                             "sessionId": session_id}))
    p.write_text("\n".join(lines) + "\n")
    return p
