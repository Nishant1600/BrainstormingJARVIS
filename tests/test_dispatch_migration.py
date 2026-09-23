import importlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def migrated(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()

    conn = sqlite3.connect(str(tmp_path / "jarvis.db"))
    conn.executescript("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            project_path TEXT NOT NULL,
            original_prompt TEXT NOT NULL,
            refined_prompt TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            claude_response TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        );
    """)
    now = time.time()
    for name, status in [
        ("a", "pending"), ("b", "building"), ("c", "planning"),
        ("d", "completed"), ("e", "failed"), ("f", "timeout"),
    ]:
        conn.execute(
            "INSERT INTO dispatches (project_name, project_path, original_prompt, "
            "status, claude_response, summary, created_at, updated_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, f"/tmp/{name}", f"prompt {name}", status, "resp", "summ",
             now, now, now))
    conn.commit()
    conn.close()

    mod = importlib.import_module("migrations.001_dispatches_to_runs")
    importlib.reload(mod)
    return mod, run_store


def test_migrate_moves_every_row(migrated):
    mod, run_store = migrated
    assert mod.migrate() == 6
    assert len(run_store.list_runs(limit=100)) == 6


def test_status_mapping(migrated):
    mod, run_store = migrated
    mod.migrate()
    by_project = {r["project_name"]: r["status"] for r in run_store.list_runs(limit=100)}
    assert by_project["a"] == run_store.RunStatus.QUEUED
    assert by_project["b"] == run_store.RunStatus.RUNNING
    assert by_project["c"] == run_store.RunStatus.RUNNING
    assert by_project["d"] == run_store.RunStatus.SUCCEEDED
    assert by_project["e"] == run_store.RunStatus.FAILED
    assert by_project["f"] == run_store.RunStatus.TIMED_OUT


def test_migrate_is_idempotent(migrated):
    mod, run_store = migrated
    assert mod.migrate() == 6
    assert mod.migrate() == 0
    assert len(run_store.list_runs(limit=100)) == 6


def test_migrate_preserves_prompt_and_origin(migrated):
    mod, run_store = migrated
    mod.migrate()
    run = [r for r in run_store.list_runs(limit=100) if r["project_name"] == "d"][0]
    assert run["prompt"] == "prompt d"
    assert run["origin"] == "voice"
    assert run["result_text"] == "resp"


def test_migrate_noop_without_dispatches_table(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()
    mod = importlib.import_module("migrations.001_dispatches_to_runs")
    importlib.reload(mod)
    assert mod.migrate() == 0
