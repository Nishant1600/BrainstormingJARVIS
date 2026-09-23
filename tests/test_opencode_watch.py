# tests/test_opencode_watch.py
"""opencode_watch: live sessions out of opencode.db.

The derived layer (naming, primary, resolve, Snapshot, transitions) is
session_watch's and is covered there. These tests cover only the source
layer: which rows become conversations, which states they get, and what
is left out — all against synthetic databases, never the developer's
real opencode.db.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import opencode_watch as ow
import session_watch as sw

NOW_MS = 1790140000000
NOW = NOW_MS / 1000.0

SCHEMA = """
CREATE TABLE session (id TEXT, directory TEXT, title TEXT, slug TEXT,
    agent TEXT, time_created INTEGER, time_updated INTEGER,
    time_archived INTEGER, project_id TEXT, permission TEXT);
CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE permission (id TEXT, project_id TEXT, action TEXT,
    resource TEXT);
"""


_DB_COUNTER = 0


def _db(tmp_path, sessions=(), messages=(), parts=(), permissions=(),
        noschema=False):
    """A throwaway opencode.db. Times default to NOW_MS unless given."""
    global _DB_COUNTER
    _DB_COUNTER += 1
    path = tmp_path / f"opencode-{_DB_COUNTER}.db"
    con = sqlite3.connect(str(path))
    if not noschema:
        con.executescript(SCHEMA)
    for s in sessions:
        con.execute(
            "insert into session (id, directory, title, slug, agent,"
            " time_created, time_updated, time_archived, project_id,"
            " permission) values (?,?,?,?,?,?,?,?,?,?)", s)
    for m in messages:
        con.execute(
            "insert into message (id, session_id, time_created,"
            " time_updated, data) values (?,?,?,?,?)", m)
    for p in parts:
        con.execute(
            "insert into part (id, message_id, session_id, time_created,"
            " time_updated, data) values (?,?,?,?,?,?)", p)
    for perm in permissions:
        con.execute(
            "insert into permission (id, project_id, action, resource)"
            " values (?,?,?,?)", perm)
    con.commit()
    con.close()
    return path


def _session(sid, directory="C:/work/chitauri", age_sec=60, title="T",
             slug="s", project="p1", archived=None, permission=None):
    return (sid, directory, title, slug, "build", NOW_MS - 3600000,
            NOW_MS - age_sec * 1000, archived, project, permission)


def _msg(mid, sid, role, age_sec=60, completed=True):
    timing = {"created": NOW_MS - age_sec * 1000}
    if role == "assistant" and completed:
        timing["completed"] = NOW_MS - age_sec * 1000 + 5000
    return (mid, sid, NOW_MS - age_sec * 1000, NOW_MS - age_sec * 1000,
            json.dumps({"role": role, "time": timing}))


def _part(pid, mid, sid, kind, age_sec=60, **body):
    data = {"type": kind}
    data.update(body)
    return (pid, mid, sid, NOW_MS - age_sec * 1000, NOW_MS - age_sec * 1000,
            json.dumps(data))


def _snap(tmp_path, **kw):
    kw.setdefault("now", NOW)
    return ow.build_snapshot(**kw)


def test_missing_database_is_an_empty_snapshot_not_an_error(tmp_path):
    snap = ow.build_snapshot(db_paths=[tmp_path / "nope.db"], now=NOW)
    assert snap.sessions == []


def test_database_without_tables_is_an_empty_snapshot(tmp_path):
    snap = _snap(tmp_path, db_paths=[_db(tmp_path, noschema=True)])
    assert snap.sessions == []


def test_session_with_no_messages_is_fresh_and_never_announced(tmp_path):
    path = _db(tmp_path, sessions=[_session("ses_1")])
    snap = _snap(tmp_path, db_paths=[path])
    assert len(snap.sessions) == 1
    s = snap.sessions[0]
    assert s.state == sw.FRESH
    assert s.announceable is False


def test_in_flight_assistant_message_is_working(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1")],
        messages=[
            _msg("m1", "ses_1", "user", age_sec=120),
            _msg("m2", "ses_1", "assistant", age_sec=60, completed=False),
        ],
        parts=[
            _part("p1", "m1", "ses_1", "text", age_sec=120,
                   text="scan it"),
        ])
    snap = _snap(tmp_path, db_paths=[path])
    (s,) = snap.sessions
    assert s.state == sw.WORKING
    assert s.last_prompt == "scan it"
    # Continuable, not socket-bound: every listed session takes a turn.
    assert s.steerable is True and s.socket_path is None and s.pids == []


def test_completed_session_is_idle_with_a_recap(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1", title="Fix it")],
        messages=[
            _msg("m1", "ses_1", "user", age_sec=300),
            _msg("m2", "ses_1", "assistant", age_sec=200),
        ],
        parts=[
            _part("p1", "m1", "ses_1", "text", age_sec=300,
                   text="fix the redirect"),
            _part("p2", "m2", "ses_1", "text", age_sec=200,
                   text="done, shipped it"),
            _part("p3", "m2", "ses_1", "tool", age_sec=210,
                   tool="edit"),
            _part("p4", "m2", "ses_1", "tool", age_sec=205,
                   tool="bash"),
        ])
    snap = _snap(tmp_path, db_paths=[path])
    (s,) = snap.sessions
    assert s.state == sw.IDLE
    assert s.title == "Fix it"
    assert s.last_prompt == "fix the redirect"
    assert s.last_text == "done, shipped it"
    assert s.recent_tools == ["edit", "bash"]
    assert s.project == "chitauri"


def test_question_as_last_word_needs_you(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1")],
        messages=[_msg("m1", "ses_1", "assistant", age_sec=60)],
        parts=[_part("p1", "m1", "ses_1", "text", age_sec=60,
                     text="Dark theme or light?")])
    (s,) = _snap(tmp_path, db_paths=[path]).sessions
    assert s.state == sw.NEEDS_YOU


def test_pending_permission_needs_you(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1", project="p1")],
        messages=[_msg("m1", "ses_1", "assistant", age_sec=60)],
        parts=[_part("p1", "m1", "ses_1", "text", age_sec=60,
                     text="working on it")],
        permissions=[("perm_1", "p1", "edit approval", "/x/y.ts")])
    (s,) = _snap(tmp_path, db_paths=[path]).sessions
    assert s.state == sw.NEEDS_YOU
    assert s.needs is not None and "edit approval" in s.needs


def test_archived_sessions_are_history_not_work(tmp_path):
    path = _db(tmp_path, sessions=[
        _session("ses_old", age_sec=60, archived=NOW_MS - 1000),
        _session("ses_live", age_sec=60),
    ], messages=[_msg("m1", "ses_live", "assistant", age_sec=60)])
    snap = _snap(tmp_path, db_paths=[path])
    assert [s.session_id for s in snap.sessions] == ["ses_live"]


def test_quiet_sessions_age_out_of_the_snapshot(tmp_path):
    path = _db(tmp_path, sessions=[
        _session("ses_quiet", age_sec=3600),
        _session("ses_live", age_sec=60),
    ], messages=[
        _msg("m1", "ses_quiet", "assistant", age_sec=3600),
        _msg("m2", "ses_live", "assistant", age_sec=60),
    ])
    snap = _snap(tmp_path, db_paths=[path])
    assert [s.session_id for s in snap.sessions] == ["ses_live"]


def test_old_need_you_is_remembered_within_retention(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1", age_sec=3600)],
        messages=[_msg("m1", "ses_1", "assistant", age_sec=3600)],
        parts=[_part("p1", "m1", "ses_1", "text", age_sec=3600,
                     text="Shall I proceed?")])
    (s,) = _snap(tmp_path, db_paths=[path]).sessions
    assert s.state == sw.NEEDS_YOU


def test_brain_home_sessions_are_not_user_conversations(tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    brain_home = str(tmp_path / "data" / "jarvis")
    path = _db(tmp_path, sessions=[
        _session("ses_brain", directory=brain_home),
        _session("ses_user", directory="C:/work/webapp"),
    ], messages=[
        _msg("m1", "ses_brain", "assistant", age_sec=60),
        _msg("m2", "ses_user", "assistant", age_sec=60),
    ])
    snap = _snap(tmp_path, db_paths=[path])
    assert [s.session_id for s in snap.sessions] == ["ses_user"]


def test_malformed_rows_do_not_cost_the_snapshot(tmp_path):
    """Parity with the base class: a row that reads as garbage still counts
    as a transcript (IDLE, uninformative) — only unreadable SOURCES are
    skipped (see the noschema test above). What matters is that nothing
    raises and the good conversation is intact."""
    path = _db(
        tmp_path,
        sessions=[
            _session("ses_bad", directory=""),
            _session("ses_good"),
        ],
        messages=[
            ("m1", "ses_bad", NOW_MS, NOW_MS, "{not json"),
            _msg("m2", "ses_good", "assistant", age_sec=60),
        ])
    snap = _snap(tmp_path, db_paths=[path])
    by_id = {s.session_id: s for s in snap.sessions}
    assert by_id["ses_good"].state == sw.IDLE
    assert by_id["ses_bad"].state == sw.IDLE


def test_a_prompt_with_no_answer_yet_is_working(tmp_path):
    """The user message went in and nothing has answered it: a turn is due,
    which reads as working rather than idle."""
    path = _db(
        tmp_path,
        sessions=[_session("ses_1")],
        messages=[
            _msg("m1", "ses_1", "assistant", age_sec=300),
            _msg("m2", "ses_1", "user", age_sec=60),
        ])
    (s,) = _snap(tmp_path, db_paths=[path]).sessions
    assert s.state == sw.WORKING


def test_session_permission_column_needs_you(tmp_path):
    path = _db(
        tmp_path,
        sessions=[_session("ses_1", permission="dialog open: approve edit?")],
        messages=[_msg("m1", "ses_1", "assistant", age_sec=60)],
        parts=[_part("p1", "m1", "ses_1", "text", age_sec=60,
                     text="working on it")])
    (s,) = _snap(tmp_path, db_paths=[path]).sessions
    assert s.state == sw.NEEDS_YOU
    assert "approve edit" in (s.needs or "")


def test_voice_names_and_resolve_work_across_sessions(tmp_path):
    path = _db(tmp_path, sessions=[
        _session("ses_1", directory="C:/work/chitauri", age_sec=60),
        _session("ses_2", directory="C:/other/hammer", age_sec=60),
    ], messages=[
        _msg("m1", "ses_1", "assistant", age_sec=60),
        _msg("m2", "ses_2", "assistant", age_sec=60),
    ])
    snap = _snap(tmp_path, db_paths=[path])
    assert snap.resolve("chitauri")[0].session_id == "ses_1"
    assert snap.needing_you() == []


def test_watcher_announces_a_finished_session(tmp_path):
    """The shared transition machinery works off opencode states: a
    session that worked for a while and then went quiet is announced
    once, like any other finished conversation."""
    w = ow.OpencodeWatcher(
        db_paths=[_db(tmp_path, sessions=[_session("ses_1")],
                      messages=[_msg("m1", "ses_1", "assistant",
                                     age_sec=120, completed=False)])])
    events = []
    w.on_event(events.append)
    w.poll_once(now=NOW - 60)
    assert events == []
    w.db_paths = [_db(tmp_path, sessions=[_session("ses_1")],
                       messages=[_msg("m1", "ses_1", "assistant",
                                      age_sec=120, completed=True)],
                       parts=[_part("p1", "m1", "ses_1", "tool",
                                    age_sec=110, tool="bash")])]
    w.poll_once(now=NOW)
    kinds = [e["kind"] for e in events]
    assert "finished" in kinds
