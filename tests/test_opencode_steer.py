# tests/test_opencode_steer.py
"""opencode_steer: one prompt into a live session, via a detached
`opencode run --session <sid> --continue` child.

The child is real (a recording fake binary) but never awaited: SENT means
the spawn succeeded, and the marker file proves what argv it was spawned
with. Nothing here spends provider quota and nothing touches the network.
"""
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import opencode_steer


_DB_COUNTER = 0


def _db(tmp_path, name=None, rows=()):
    """A minimal sessions database: id, directory, archived-or-not."""
    global _DB_COUNTER
    _DB_COUNTER += 1
    path = tmp_path / (name or f"steer-{_DB_COUNTER}.db")
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE session (id TEXT, directory TEXT,"
                " time_archived INTEGER, project_id TEXT)")
    for sid, directory, archived in rows:
        con.execute("insert into session (id, directory, time_archived,"
                    " project_id) values (?,?,?,?)",
                    (sid, directory, archived, "p1"))
    con.commit()
    con.close()
    return path


def _fake(tmp_path):
    """A stand-in `opencode` that records its argv and exits at once."""
    script = tmp_path / "fake_opencode_steer.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "marker = os.getenv('FAKESTEER_MARKER')\n"
        "if marker:\n"
        "    open(marker, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "sys.exit(int(os.getenv('FAKESTEER_EXIT', '0')))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _live_db(tmp_path):
    """One live session whose directory really exists: Popen validates
    cwd, so a fictional path would fail the spawn for the wrong reason."""
    project = tmp_path / "chitauri"
    project.mkdir(exist_ok=True)
    return _db(tmp_path, rows=[("ses_live", str(project), None)])


def _live_dir(tmp_path):
    return str(tmp_path / "chitauri")


def _wait_for(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


# --- vocabulary -----------------------------------------------------------

def test_outcomes_match_the_socket_steerer():
    """The server keys its speech off these strings; a steer is a steer
    whichever backend carries it."""
    import session_steer
    assert opencode_steer.SENT == session_steer.SENT
    assert opencode_steer.NOT_LIVE == session_steer.NOT_LIVE
    assert opencode_steer.REFUSED == session_steer.REFUSED
    assert opencode_steer.FAILED == session_steer.FAILED


# --- session_known ----------------------------------------------------------

def test_known_session_returns_its_directory(tmp_path):
    known, directory = opencode_steer.session_known(
        "ses_live", [_live_db(tmp_path)])
    assert (known, directory) == (True, _live_dir(tmp_path))


def test_unknown_session_is_not_live(tmp_path):
    assert opencode_steer.session_known("ses_nope", [_live_db(tmp_path)]) == \
        (False, "")


def test_archived_session_is_not_live(tmp_path):
    path = _db(tmp_path, rows=[("ses_old", "C:/work/old", 1790000000000)])
    assert opencode_steer.session_known("ses_old", [path]) == (False, "")


def test_session_without_a_directory_is_not_live(tmp_path):
    path = _db(tmp_path, rows=[("ses_nodir", "", None)])
    assert opencode_steer.session_known("ses_nodir", [path]) == (False, "")


def test_garbage_ids_and_missing_databases_are_not_live(tmp_path):
    assert opencode_steer.session_known("", [_live_db(tmp_path)]) == \
        (False, "")
    assert opencode_steer.session_known(None, [_live_db(tmp_path)]) == \
        (False, "")
    assert opencode_steer.session_known("ses_live", [tmp_path / "nope.db"]) == \
        (False, "")


# --- post_to_session ----------------------------------------------------------

def test_empty_prompt_is_refused_without_touching_anything(tmp_path):
    assert opencode_steer.post_to_session(
        "ses_live", "   ", db_paths=[tmp_path / "nope.db"]) == "refused"


def test_unknown_session_spawns_nothing(tmp_path, monkeypatch):
    marker = tmp_path / "argv.json"
    monkeypatch.setenv("FAKESTEER_MARKER", str(marker))
    outcome = opencode_steer.post_to_session(
        "ses_nope", "carry on", opencode_path=sys.executable,
        opencode_argv_prefix=[_fake(tmp_path)],
        db_paths=[_live_db(tmp_path)])
    assert outcome == "not_live"
    assert not marker.exists()


def test_prompt_is_staged_as_a_continue_turn(tmp_path, monkeypatch):
    marker = tmp_path / "argv.json"
    monkeypatch.setenv("FAKESTEER_MARKER", str(marker))
    outcome = opencode_steer.post_to_session(
        "ses_live", "carry on with the redirect",
        opencode_path=sys.executable,
        opencode_argv_prefix=[_fake(tmp_path)],
        db_paths=[_live_db(tmp_path)])
    assert outcome == "sent"
    assert _wait_for(marker), "the detached child never started"
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[argv.index("--session") + 1] == "ses_live"
    assert "--continue" in argv
    assert argv[argv.index("--dir") + 1] == _live_dir(tmp_path)
    assert argv[-1] == "carry on with the redirect"
    # Inherits the session's model and posture: nothing here may rename,
    # re-model, or auto-approve inside somebody else's session.
    assert "--model" not in argv
    assert "--auto" not in argv
    assert "--title" not in argv


def test_unspawnable_binary_is_failed_not_live(tmp_path):
    outcome = opencode_steer.post_to_session(
        "ses_live", "carry on", opencode_path="/nonexistent/opencode",
        db_paths=[_live_db(tmp_path)])
    assert outcome == "failed"
