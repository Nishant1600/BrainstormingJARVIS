"""Post a message into a live opencode session.

The wire is `opencode run --session <sid> --continue <prompt>`: the prompt
lands in the session as a user turn, which the model then answers. There
is no inbox socket to post to (see session_steer.py for the claude
backend), so delivery here means SPAWNING — with consequences the contract
below makes explicit rather than hiding:

* SENT is fast. The child is spawned detached and the call returns once
  the spawn succeeds; the reply is NOT awaited. A turn takes tens of
  seconds, and awaiting it here would blow straight past the 20 s budget
  jarvis_mcp's TIMEOUT_SEC exists to enforce. Like the socket's SENT, it
  proves the message left this process, not that the session answered.
* The session is validated BEFORE spawning: an unknown or archived sid is
  NOT_LIVE without spawning anything, and a session with no known
  directory is NOT_LIVE too — continuing under the wrong directory hangs
  the CLI instead of answering (measured, twice), so there is no
  "continue from wherever" fallback.
* No `--auto` and no `--model`: the turn inherits the session's own model
  and approval posture. A steered turn that needs approval parks it where
  the session lives (its TUI), exactly as a socket-delivered message used
  to wait on that session's own permission flow. Auto-approving inside
  somebody else's session is not this tool's call, and renaming or
  re-modelling it mid-conversation even less so.
* No `--title` either, for the same reason: the title names the session
  in `opencode session list`, and a steer must not rename it.
* stdout/stderr go to DEVNULL: nobody reads this child's stream (its
  reply lands in the session record, which the watcher already reads),
  and pipes nobody drains are deadlocks waiting to happen.
* Children are reaped by a daemon waiter each, so no zombies accumulate;
  a non-zero exit is logged at debug with the session id. There is no
  timeout: a turn's length is the session's business, and killing the
  local child would not unsend the message anyway.

Return vocabulary is identical to session_steer's (`sent`, `not_live`,
`refused`, `failed`) on purpose: the server's speech branches key off
those strings, and a steer is a steer whichever backend carries it.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

from opencode_watch import default_db_paths

log = logging.getLogger("jarvis.opencode_steer")

SENT = "sent"          # the child spawned — NOT that the session replied;
                       # no reply is ever awaited, by design (see above)
NOT_LIVE = "not_live"  # no such session, it is archived, or it has no
                       # directory to continue from: nothing to talk to
REFUSED = "refused"    # empty prompt — never spawn on those
FAILED = "failed"      # the spawn itself failed


def _auth_db_paths(db_paths: list[Path] | None = None) -> list[Path]:
    """Where to look for the session, in priority order. Tests pass tmp
    databases; production uses opencode's own."""
    if db_paths is not None:
        return list(db_paths)
    return default_db_paths()


def session_known(session_id: str,
                  db_paths: list[Path] | None = None) -> tuple[bool, str]:
    """(True, directory) when `session_id` names a live session, else
    (False, ""). Live means present and not archived; the directory is
    what the continue-run must run under. Never raises."""
    if not session_id or not isinstance(session_id, str):
        return False, ""
    for path in _auth_db_paths(db_paths):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                  timeout=2.0)
        except (sqlite3.Error, OSError, ValueError):
            continue
        try:
            try:
                row = con.execute(
                    "select directory from session where id = ?"
                    " and time_archived is null",
                    (session_id,)).fetchone()
            except (sqlite3.Error, OSError, ValueError):
                continue
            if row is None:
                return False, ""
            directory = row[0] if isinstance(row[0], str) else ""
            return (True, directory) if directory.strip() else (False, "")
        finally:
            try:
                con.close()
            except Exception:
                pass
    return False, ""


def _binary(opencode_path: str | None = None) -> str:
    """The binary, verbatim argv[0] — never split, so a path with spaces
    (Windows Program Files, npm shims) survives intact."""
    return (opencode_path or os.getenv("JARVIS_OPENCODE_PATH")
            or shutil.which("opencode") or "opencode")


def _reap(proc: subprocess.Popen, session_id: str) -> None:
    """Wait out one detached child off-thread, then drop it. Daemon, so a
    server shutdown is never held open by a steer still talking."""
    try:
        code = proc.wait()
    except Exception as e:
        log.warning(f"opencode steer to {session_id} unsettled: {e}")
        return
    if code not in (0, None):
        log.warning(f"opencode steer to {session_id} exited {code}")


def post_to_session(session_id: str | None, prompt: str | None,
                    opencode_path: str | None = None,
                    opencode_argv_prefix: list[str] | None = None,
                    db_paths: list[Path] | None = None) -> str:
    """Deliver one prompt into a live session. Returns `sent`, `not_live`,
    `refused`, or `failed`. Never raises: like the socket version, every
    failure mode is a return value, because the caller speaks them aloud.

    `opencode_argv_prefix` is prepended verbatim ahead of the `run`
    subcommand — the seam tests stand a script in for the binary through,
    without a shell (same pattern as the executor and the brain).
    """
    if not prompt or not str(prompt).strip():
        return REFUSED
    known, directory = session_known(session_id or "", db_paths)
    if not known:
        return NOT_LIVE
    argv = ([_binary(opencode_path)] + list(opencode_argv_prefix or [])
            + ["run", "--session", str(session_id), "--continue",
               "--dir", directory, str(prompt).strip()])
    try:
        proc = subprocess.Popen(
            argv, cwd=directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True)
    except OSError as e:
        log.warning(f"opencode steer to {session_id} could not spawn: {e}")
        return FAILED
    thread = threading.Thread(target=_reap, args=(proc, str(session_id)),
                              name=f"jarvis-steer-{session_id}",
                              daemon=True)
    thread.start()
    return SENT
