"""/api/usage/limits — what the masthead gauges read from.

The endpoint's whole job is to be honest about what it does not know: on a
fresh machine the brain has taken no turn, so there is no observation, and
the answer must say so rather than serve zeroes that render as empty gauges.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import usage_store
    importlib.reload(usage_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    with TestClient(server.app) as c:
        yield c, usage_store


def test_no_observation_reports_no_numbers(client):
    c, _ = client
    body = c.get("/api/usage/limits").json()
    assert body["measured"] is False
    assert body["observed_at"] is None
    assert [w["key"] for w in body["windows"]] == ["five_hour", "seven_day"]
    assert all(w["utilization"] is None for w in body["windows"])


def test_an_observation_is_served_with_its_age(client):
    c, usage_store = client
    usage_store.record({
        "status": "allowed_warning",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.62, "resetsAt": 4102444800},
            "seven_day": {"utilization": 0.84, "resetsAt": 4102531200},
        },
    })
    body = c.get("/api/usage/limits").json()
    assert body["measured"] is True
    assert body["status"] == "allowed_warning"
    assert body["age_sec"] < 5 and body["stale"] is False
    by_key = {w["key"]: w for w in body["windows"]}
    assert by_key["five_hour"]["utilization"] == 62.0
    assert by_key["seven_day"]["utilization"] == 84.0
    assert by_key["seven_day"]["resets_at"] == 4102531200


def test_the_legacy_usage_endpoint_still_answers(client):
    """/api/usage is someone else's (the old token log). The limits endpoint
    lives beside it rather than shadowing it."""
    c, _ = client
    assert c.get("/api/usage").status_code == 200
