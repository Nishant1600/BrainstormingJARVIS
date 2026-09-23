import importlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()
    return run_store


def test_create_run_returns_uuid(store):
    run_id = store.create_run("build a thing", "proj", "/tmp/proj", "voice")
    assert len(run_id) == 36
    assert run_id.count("-") == 4


def test_created_run_starts_queued(store):
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    run = store.get_run(run_id)
    assert run["status"] == store.RunStatus.QUEUED
    assert run["prompt"] == "build"
    assert run["origin"] == "voice"
    assert run["cost_usd"] == 0
    assert run["started_at"] is None


def test_get_run_missing_returns_none(store):
    assert store.get_run("nope") is None


def test_update_run_sets_fields(store):
    run_id = store.create_run("build", "proj", "/tmp/proj", "api")
    store.update_run(run_id, status=store.RunStatus.RUNNING, pid=4242)
    run = store.get_run(run_id)
    assert run["status"] == store.RunStatus.RUNNING
    assert run["pid"] == 4242


def test_update_run_rejects_unknown_column(store):
    run_id = store.create_run("build", "proj", "/tmp/proj", "api")
    with pytest.raises(ValueError):
        store.update_run(run_id, bogus_column="x")


def test_list_runs_newest_first(store):
    first = store.create_run("a", "p", "/tmp/p", "voice")
    time.sleep(0.01)
    second = store.create_run("b", "p", "/tmp/p", "voice")
    runs = store.list_runs()
    assert [r["id"] for r in runs] == [second, first]


def test_list_runs_filters_by_status(store):
    a = store.create_run("a", "p", "/tmp/p", "voice")
    store.create_run("b", "p", "/tmp/p", "voice")
    store.update_run(a, status=store.RunStatus.SUCCEEDED)
    runs = store.list_runs(status=[store.RunStatus.SUCCEEDED])
    assert [r["id"] for r in runs] == [a]


def test_list_runs_filters_by_project(store):
    a = store.create_run("a", "alpha", "/tmp/a", "voice")
    store.create_run("b", "beta", "/tmp/b", "voice")
    runs = store.list_runs(project="alpha")
    assert [r["id"] for r in runs] == [a]


def test_events_round_trip_in_seq_order(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    for i in (1, 2, 3):
        store.append_event(run_id, i, "assistant", f'{{"n":{i}}}')
    events = store.get_events(run_id)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[0]["kind"] == "assistant"


def test_get_events_after_seq(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    for i in (1, 2, 3):
        store.append_event(run_id, i, "assistant", "{}")
    assert [e["seq"] for e in store.get_events(run_id, after_seq=1)] == [2, 3]


def test_next_seq_increments(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    assert store.next_seq(run_id) == 1
    store.append_event(run_id, 1, "system", "{}")
    assert store.next_seq(run_id) == 2


def test_count_events(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    assert store.count_events(run_id) == 0
    for i in (1, 2, 3):
        store.append_event(run_id, i, "assistant", "{}")
    assert store.count_events(run_id) == 3


def test_sweep_marks_running_as_failed(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    store.update_run(run_id, status=store.RunStatus.RUNNING)
    assert store.sweep_stale_runs() == 1
    run = store.get_run(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert "server restarted" in run["error"]
    assert run["ended_at"] is not None


def test_sweep_leaves_terminal_runs_alone(store):
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    store.update_run(run_id, status=store.RunStatus.SUCCEEDED)
    assert store.sweep_stale_runs() == 0
    assert store.get_run(run_id)["status"] == store.RunStatus.SUCCEEDED


def test_stats_aggregates_cost_and_counts(store):
    a = store.create_run("a", "p", "/tmp/p", "voice")
    b = store.create_run("b", "p", "/tmp/p", "voice")
    store.update_run(a, status=store.RunStatus.SUCCEEDED, cost_usd=0.25, input_tokens=100)
    store.update_run(b, status=store.RunStatus.FAILED, cost_usd=0.75, input_tokens=50)
    s = store.stats("day")
    assert s["total_cost_usd"] == 1.0
    assert s["by_status"][store.RunStatus.SUCCEEDED] == 1
    assert s["by_status"][store.RunStatus.FAILED] == 1
    assert s["total_input_tokens"] == 150


def test_requested_model_round_trips(store):
    run_id = store.create_run("build", "proj", "/tmp/proj", "api")
    store.update_run(run_id, requested_model="haiku")
    run = store.get_run(run_id)
    assert run["requested_model"] == "haiku"


# -- schema-migration safety: a live jarvis.db predating `requested_model` --
# CREATE TABLE IF NOT EXISTS never alters an existing table, so init_db()
# must backfill the column onto a database built under the old schema
# shape (mirrors migrations/001_dispatches_to_runs.py's PRAGMA-table_info-
# then-ALTER pattern) without disturbing rows already there.

_OLD_RUNS_SCHEMA = """
    CREATE TABLE runs (
        id            TEXT PRIMARY KEY,
        project_name  TEXT NOT NULL,
        project_path  TEXT NOT NULL,
        prompt        TEXT NOT NULL,
        origin        TEXT NOT NULL,
        status        TEXT NOT NULL,
        resume_from   TEXT,
        result_text   TEXT DEFAULT '',
        summary       TEXT DEFAULT '',
        error         TEXT DEFAULT '',
        exit_code     INTEGER,
        pid           INTEGER,
        cost_usd      REAL    DEFAULT 0,
        input_tokens  INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens     INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        num_turns     INTEGER DEFAULT 0,
        model         TEXT DEFAULT '',
        created_at    REAL NOT NULL,
        started_at    REAL,
        ended_at      REAL
    );
"""


def test_existing_database_without_requested_model_still_opens(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)

    # Build a jarvis.db in the pre-`requested_model` shape directly,
    # bypassing init_db() entirely — this is what a live database looks
    # like the moment before this change ships.
    conn = sqlite3.connect(str(data_paths.db_path()))
    conn.executescript(_OLD_RUNS_SCHEMA)
    conn.execute(
        "INSERT INTO runs (id, project_name, project_path, prompt, origin, "
        "status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("old-run", "proj", "/tmp/proj", "legacy prompt", "voice",
         "succeeded", time.time()))
    conn.commit()
    conn.close()

    # Must not raise against a table that predates the column.
    run_store.init_db()

    old = run_store.get_run("old-run")
    assert old is not None
    assert old["prompt"] == "legacy prompt"
    assert old["requested_model"] == ""

    new_id = run_store.create_run("new prompt", "proj", "/tmp/proj", "voice")
    run_store.update_run(new_id, requested_model="opus")
    assert run_store.get_run(new_id)["requested_model"] == "opus"


# -- `is_error`: the CLI's own verdict on the turn, kept ---------------------
# The CLI reports an auth failure as `subtype: "success"` with
# `is_error: true`, AND exits 0. Deciding a run's fate on the exit code alone
# recorded that as `succeeded`. The flag is now a column so the reading is
# not lost the moment the stream is over.


def test_is_error_defaults_to_zero_and_round_trips(store):
    run_id = store.create_run("p", "proj", "/tmp/proj", "voice")
    assert store.get_run(run_id)["is_error"] == 0
    store.update_run(run_id, is_error=1)
    assert store.get_run(run_id)["is_error"] == 1


def test_existing_database_without_is_error_still_opens(monkeypatch, tmp_path):
    """Same backfill as `requested_model`: CREATE TABLE IF NOT EXISTS never
    alters a table that already exists, so a live jarvis.db needs the ALTER."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)

    conn = sqlite3.connect(str(data_paths.db_path()))
    conn.executescript(_OLD_RUNS_SCHEMA)
    conn.execute(
        "INSERT INTO runs (id, project_name, project_path, prompt, origin, "
        "status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("old-run", "proj", "/tmp/proj", "legacy prompt", "voice",
         "succeeded", time.time()))
    conn.commit()
    conn.close()

    run_store.init_db()

    old = run_store.get_run("old-run")
    assert old is not None
    assert old["is_error"] == 0

    new_id = run_store.create_run("new prompt", "proj", "/tmp/proj", "voice")
    run_store.update_run(new_id, is_error=1)
    assert run_store.get_run(new_id)["is_error"] == 1
