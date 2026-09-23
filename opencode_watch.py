"""Watch opencode sessions: the opencode-backend source for JARVIS's session view.

`session_watch.py` reads Claude Code's roster (`~/.claude/sessions/*.json`)
and JSONL transcripts. None of that exists here: opencode keeps its
sessions in SQLite (`opencode.db`) with a `session` / `message` / `part`
schema, and this module reads THAT. Everything derived — SessionState,
voice naming, primary ranking, Snapshot, the watcher's transition
announcements — is session_watch's, imported and reused, so the two
backends name and count conversations identically.

Pinned against opencode 1.18.32. The schema is internal to opencode (no
stable contract), so every query is defensive: a missing table or column,
a locked database, or an unparseable row yields an empty snapshot and a
warning — never an exception. A snapshot that cannot be read is not a
snapshot full of nothing.

How a session's state is derived (in order):

* archived (`time_archived` set) → not listed at all. An archived session
  is history, and history is what `opencode session list` is for.
* no messages → FRESH. Never announced, exactly like the base class:
  nobody has started it, so reporting it would be nonsense.
* an assistant message with no `time.completed` → WORKING. The turn is
  still in flight.
* a pending permission (the `permission` table, or a non-empty
  `session.permission`) → NEEDS_YOU, naming the action. Permission rows
  have never been observed live; the branch is exercised with synthetic
  rows in tests.
* the last assistant text ends with a question → NEEDS_YOU. Same
  URL-stripping conservatism as the base class
  (`session_watch._looks_like_a_question`, reused, not reimplemented).
* otherwise → IDLE.

There is no SHELL and no UNKNOWN on this backend: an opencode session is
either mid-turn, waiting, fresh, or idle. The enum itself is untouched.

Liveness — what counts as "running", since sessions never exit:

* a WORKING session is always live;
* anything updated within SESSION_ACTIVE_SEC (15 min) is live;
* a NEEDS_YOU session stays listed for SESSION_NEEDS_YOU_SEC (2 h), so an
  unanswered prompt is not forgotten the moment it goes quiet.
* everything else is history and is not listed.

Two approximations, stated plainly because they decide what the user hears:

* An idle TUI left open for an hour looks exactly like a closed one in
  this database (nothing writes until the next turn). Mapping open windows
  to sessions would need per-process cwd probes; until that exists, the
  activity window is the signal.
* `origin` is always "terminal": a headless `opencode run` child and an
  interactive TUI are indistinguishable here (same agent, same directory
  shape). JARVIS's own brain session is excluded by directory, exactly
  like the base class excludes its own brain; JARVIS's own RUNS are not
  yet excluded — their opencode session ids live only in the runs'
  stored events, and teaching the snapshot about them is server-side work
  still to come. Until then a run he just started reads as one more
  conversation, which is wrong in the same direction as useful: it IS
  work in progress in that project.

Privacy: message and part texts are read to recap what a session is doing
— the same texts the base class reads out of transcripts — and surface
the same way (title, last prompt, last line, recent tools). Nothing here
runs on a timer beyond the watcher's own 1 Hz poll, and nothing is
persisted: the database is opened read-only.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import session_watch as sw

log = logging.getLogger("jarvis.opencode_watch")

# A session updated this recently is live work. See the module docstring
# for what the window cannot see (an idle-but-open TUI).
SESSION_ACTIVE_SEC = 15 * 60
# ...while an unanswered need-you keeps its session listed this long, so a
# prompt that waits an hour is still reported rather than forgotten.
SESSION_NEEDS_YOU_SEC = 2 * 60 * 60

# How many recent tool names a recap carries. Same width as the base
# class's MAX_RECENT_TOOLS, so the two backends describe sessions alike.
MAX_RECENT_TOOLS = 5

# How long a database open or query may take. opencode holds the write
# lock while it appends; a reader that waits on it stalls the voice loop
# it was polled from, so the ceiling is short and a locked database is an
# empty snapshot, not an error.
_DB_TIMEOUT_SEC = 2.0

_DB_REL = Path(".local") / "share" / "opencode" / "opencode.db"


def default_db_paths(extra: list[Path] | None = None) -> list[Path]:
    """Where opencode.db may live, in priority order.

    `Path.home() / .local/share/...` covers POSIX and Windows alike (the
    live Windows install keeps it under %USERPROFILE%\\.local\\share).
    $XDG_DATA_HOME is honoured when set. `extra` (tests, JARVIS_DATA_DIR
    isolation) goes first.
    """
    out = [Path(p).expanduser() for p in (extra or []) if str(p)]
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg:
        out.append(Path(xdg).expanduser() / "opencode" / "opencode.db")
    out.append(Path.home() / _DB_REL)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _connect(path: Path):
    """A read-only connection, or None when the database cannot be opened.

    Read-only (`mode=ro`) so a reader can never take the write lock it is
    trying to wait out, and can never modify what it came to observe.
    """
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               timeout=_DB_TIMEOUT_SEC)
    except (sqlite3.Error, OSError, ValueError) as e:
        log.warning(f"opencode sessions unreadable at {path}: {e}")
        return None


def _rows(db: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict]:
    """Rows as plain dicts, or [] when the schema does not have what the
    query asks for (a newer or older opencode than the pinned one)."""
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(query, args)]
    except (sqlite3.Error, OSError, ValueError):
        return []


def _parse(data) -> dict:
    if not isinstance(data, str) or not data:
        return {}
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ms(value) -> float | None:
    """Epoch milliseconds (the database's unit) to seconds, or None."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _is_own_brain(directory: str) -> bool:
    """True for JARVIS's own brain session, never a user conversation.

    The brain runs with cwd exactly equal to the brain home, so a session
    there is his, the way an `entrypoint: sdk-cli` roster entry with the
    brain's cwd is his on the claude backend. Compared with the same
    symlink-tolerant equality the base class uses.
    """
    try:
        return sw._same_dir(directory or "", sw.brain_cwd())
    except (TypeError, ValueError):
        return False


def _clip(text: str) -> str:
    """One line of clipped text, mirroring the base class's width."""
    one_line = " ".join(text.split())
    limit = sw.MAX_TEXT
    return one_line if len(one_line) <= limit else one_line[:limit - 1] + "…"


def _recap(db: sqlite3.Connection, session_id: str) -> sw.Recap:
    """What a session is doing, from its latest messages and parts.

    Reads newest-first and keeps the FIRST of each kind seen: the latest
    user text, the latest assistant text, and the recent tool names back
    in chronological order.
    """
    recap = sw.Recap(exists=False)
    messages = _rows(
        db, "select id, data from message where session_id = ?"
            " order by time_created desc, rowid desc limit 40",
        (session_id,))
    if not messages:
        return recap
    recap.exists = True
    by_id = {str(row.get("id")): _parse(row.get("data")) for row in messages}
    roles = {mid: data.get("role") for mid, data in by_id.items()}
    placeholders = ",".join("?" for _ in by_id)
    parts = _rows(
        db, f"select message_id, data from part where message_id"
            f" in ({placeholders}) order by time_updated desc,"
            f" rowid desc limit 80",
        tuple(by_id))
    tools: list[str] = []
    for row in parts:
        part = _parse(row.get("data"))
        role = roles.get(str(row.get("message_id")))
        kind = part.get("type")
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if role == "assistant" and recap.last_text is None:
                recap.last_text = _clip(text)
            elif role == "user" and recap.last_prompt is None:
                recap.last_prompt = _clip(text)
        elif kind == "tool" and role == "assistant":
            name = part.get("tool")
            if isinstance(name, str) and name:
                tools.append(name)
    recap.recent_tools = list(reversed(tools))[-MAX_RECENT_TOOLS:]
    return recap


def _working(db: sqlite3.Connection, session_id: str) -> bool:
    """True while a turn is in flight or one is due.

    Either an assistant message the model started and has not completed,
    or a user message newer than the latest completed assistant turn: the
    prompt went in and nothing has answered it yet. Only the NEWEST rows
    decide — anything older finished before them, or the ordering is
    untrustworthy either way.
    """
    newest_user: float | None = None
    newest_done: float | None = None
    for row in _rows(
            db, "select data from message where session_id = ?"
                " order by time_created desc limit 8", (session_id,)):
        data = _parse(row.get("data"))
        role = data.get("role")
        timing = data.get("time")
        if not isinstance(timing, dict):
            continue
        created = _ms(timing.get("created"))
        if role == "assistant":
            if timing.get("completed") is None and created is not None:
                return True
            if created is not None and (newest_done is None
                                        or created > newest_done):
                newest_done = created
        elif role == "user":
            if created is not None and (newest_user is None
                                        or created > newest_user):
                newest_user = created
        if newest_user is not None and newest_done is not None:
            break
    return (newest_user is not None
            and (newest_done is None or newest_user > newest_done))


def _pending_permission(db: sqlite3.Connection, session: dict) -> str | None:
    """What the session is waiting on a human for, or None.

    The `permission` table carries one row per outstanding approval; the
    session row's own `permission` column is taken at face value when set.
    Neither has been observed live — both branches are pinned with
    synthetic rows in tests — so this reports rather than interprets.
    """
    own = session.get("permission")
    if isinstance(own, str) and own.strip():
        return " ".join(own.split())[:200]
    if isinstance(own, dict) and own:
        return "a permission prompt"
    project_id = session.get("project_id")
    if project_id:
        for row in _rows(db, "select action, resource from permission"
                            " where project_id = ? limit 5", (project_id,)):
            action = row.get("action") or "a permission prompt"
            resource = f" on {row['resource']}" if row.get("resource") else ""
            return f"{action}{resource}"[:200]
    return None


def build_snapshot(db_paths: list[Path] | None = None,
                   now: float | None = None) -> sw.Snapshot:
    """Every live opencode conversation at one instant. Never raises."""
    import time as _time
    at = now if now is not None else _time.time()
    sessions: list[sw.SessionState] = []
    for path in db_paths if db_paths is not None else default_db_paths():
        db = _connect(path)
        if db is None:
            continue
        # The first database that OPENS wins: sources are in priority
        # order, and two of them never both hold live sessions.
        try:
            sessions = _read_snapshot(db, at)
        except Exception as e:  # the database belongs to another process
            log.warning(f"opencode snapshot failed for {path}: {e}")
            sessions = []
        finally:
            try:
                db.close()
            except Exception:
                pass
        break
    sw._assign_voice_names(sessions)
    sw._mark_primary(sessions)
    sessions.sort(key=lambda s: (s.project, s.voice_name))
    return sw.Snapshot(sessions=sessions, taken_at=at)


def _read_snapshot(db: sqlite3.Connection, now: float) -> list[sw.SessionState]:
    """One database's live conversations. Never raises."""
    out: list[sw.SessionState] = []
    # The history cutoff first, in SQL: rows older than anything the
    # liveness policy could keep are never read per-row below. A busy
    # machine holds hundreds of dead sessions; each would otherwise cost
    # a message scan and a recap on every 1 Hz poll.
    cutoff_ms = int((now - SESSION_NEEDS_YOU_SEC) * 1000)
    for session in _rows(
            db, "select id, directory, title, slug, agent, time_created,"
                " time_updated, time_archived, project_id, permission"
                " from session where time_archived is null"
                " and time_updated >= ?"
                " order by time_updated desc limit 200", (cutoff_ms,)):
        try:
            state = _one_session(db, session, now)
        except Exception as e:  # one bad row must not cost the snapshot
            log.warning(f"opencode session unreadable: {e}")
            continue
        if state is not None:
            out.append(state)
    return out


def _one_session(db: sqlite3.Connection, session: dict,
                 now: float) -> sw.SessionState | None:
    """One row's conversation, or None when it is history, not work."""
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    directory = str(session.get("directory") or "")
    if _is_own_brain(directory):
        return None
    updated = _ms(session.get("time_updated"))
    if updated is None:
        return None
    working = _working(db, session_id)
    needs = _pending_permission(db, session)
    age = now - updated
    recap = _recap(db, session_id)
    # A question is a need with no reason attached: the state below says
    # NEEDS_YOU while `needs` stays None, exactly like the base class.
    question = (needs is None
                and sw._looks_like_a_question(recap.last_text))
    if not working and age > SESSION_ACTIVE_SEC:
        if needs is None and not question:
            return None  # history: quiet, finished, and needing nothing
        if age > SESSION_NEEDS_YOU_SEC:
            return None
    if not recap.exists:
        state, needs = sw.FRESH, None
    elif working:
        state, needs = sw.WORKING, None
    elif needs is not None:
        state = sw.NEEDS_YOU
    elif question:
        state, needs = sw.NEEDS_YOU, None
    else:
        state, needs = sw.IDLE, None
    title = session.get("title")
    title = title if isinstance(title, str) and title.strip() else None
    project = sw.project_name(directory) if directory else session_id[:8]
    return sw.SessionState(
        session_id=session_id,
        cwd=directory,
        project=project,
        state=state,
        pids=[],
        primary_pid=None,
        roster_name=str(session.get("slug") or ""),
        needs=needs,
        title=title,
        last_prompt=recap.last_prompt,
        last_text=recap.last_text,
        recent_tools=recap.recent_tools,
        started=_ms(session.get("time_created")),
        since=updated,
        origin="terminal",
        # Every listed session takes a continue-turn, so every one is
        # steerable; there is no inbox socket to check for. `socket_path`
        # stays None, and the send keys off the session id instead.
        steerable=True,
        socket_path=None,
    )


class OpencodeWatcher(sw.SessionWatcher):
    """The watcher's transitions over opencode sessions.

    Polling, gone-cache retention, naming, primary ranking and event
    publishing are all the base class's: only the snapshot source differs.
    """

    def __init__(self, db_paths: list[Path] | None = None,
                 interval: float = 1.0):
        super().__init__(roots=None, interval=interval)
        self.db_paths = db_paths

    def build_snapshot(self, now: float) -> sw.Snapshot:
        return build_snapshot(db_paths=self.db_paths, now=now)
