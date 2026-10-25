import json
import time
import pytest
from datetime import datetime, UTC, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finbot.core.auth.session import SessionContext
from finbot.core.auth.middleware import get_session_context
from finbot.experiments.store import Store
from finbot.experiments.api import router, get_store
from finbot.experiments.schemas import ExperimentConfig


def test_store_respects_application_dotenv_settings(tmp_path, monkeypatch):
    from finbot.config import settings

    monkeypatch.delenv("FINBOT_EXPERIMENTS_DB", raising=False)
    expected = tmp_path / "configured" / "experiments.db"
    monkeypatch.setattr(settings, "FINBOT_EXPERIMENTS_DB", str(expected))
    assert Store().path == expected


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "experiments.db")


def connection(store, who="a"):
    return store.save_connection(
        who, {"name": "test", "kind": "mock", "base_url": "", "api_key": "secret-never-export"}
    )


def job(store, who="a", key=None):
    c = connection(store, who)
    return store.create_job(
        who,
        "experiment",
        {"connection_id": c["id"], "model": "demo"},
        [{"id": "case", "scenario_id": "baseline_clean", "repeat": 1, "status": "queued"}],
        idempotency_key=key,
    )


def test_credentials_are_encrypted_and_private(store):
    c = connection(store)
    assert "api_key" not in c and "credential" not in c and c["has_api_key"]
    assert store.get_connection("a", c["id"], True)["api_key"] == "secret-never-export"
    with pytest.raises(KeyError):
        store.get_connection("b", c["id"], True)
    with store.db() as db:
        raw = db.execute("SELECT data FROM connections").fetchone()[0]
    assert "secret-never-export" not in raw
    assert store.key_path.stat().st_mode & 0o777 == 0o600


def test_idempotency_claim_and_immutable_snapshot(store):
    a = job(store, key="same-key")
    b = job(store, key="same-key")
    assert a["id"] == b["id"]
    claim = store.claim_job("worker")
    assert claim["connection"]["api_key"] == "secret-never-export"
    assert store.claim_job("second-worker") is None
    with pytest.raises(ValueError):
        store.update_job(a["id"], config={})
    assert "credential" not in json.dumps(store.get_job(a["id"]))
    assert "secret-never-export" not in json.dumps(store.list_jobs("a"))


def test_cancel_does_not_lose_finished_cases(store):
    a = job(store)
    store.claim_job("worker")
    store.update_case(a["id"], "case", status="completed", result={"test": True})
    store.cancel("a", a["id"])
    assert store.cancelled(a["id"])
    assert store.get_job(a["id"])["cases"][0]["result"] == {"test": True}
    with pytest.raises(KeyError):
        store.cancel("b", a["id"])


def test_recovery_never_requeues_paid_attempts(store):
    a = job(store)
    store.heartbeat("worker")
    store.claim_job("worker")
    store.update_case(a["id"], "case", status="running")
    assert store.recover_stale_jobs() == 0
    with store.db() as db:
        db.execute("UPDATE workers SET seen=?", (time.time() - 200,))
    assert store.recover_stale_jobs() == 1
    assert store.get_job(a["id"])["status"] == "interrupted"
    assert store.claim_job("new-worker") is None
    assert store.get_job(a["id"])["cases"][0]["status"] == "interrupted"


def test_configuration_bounds_and_unknown_prices():
    c = ExperimentConfig(connection_id="x", model="m")
    assert c.temperature is None and c.input_price is None
    for change in [
        {"repeats": 0},
        {"temperature": float("nan")},
        {"input_price": 1},
        {"scenario_ids": ["a", "a"]},
    ]:
        with pytest.raises(ValueError):
            ExperimentConfig(connection_id="x", model="m", **change)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(router)
    now = datetime.now(UTC)
    ctx = SessionContext("session", "user", False, "ns", now, now + timedelta(days=1))
    app.dependency_overrides[get_session_context] = lambda: ctx
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def test_api_lifecycle_preserves_snapshot_and_hides_keys(client, store):
    c = client.post(
        "/api/v1/experiments/connections",
        json={"name": "Mock", "kind": "mock", "api_key": "never-show"},
    ).json()
    assert c["has_api_key"] and "api_key" not in c
    cfg = {
        "name": "Test",
        "connection_id": c["id"],
        "model": "mock",
        "scenario_ids": ["baseline_clean"],
    }
    saved = client.post("/api/v1/experiments/configs", json=cfg)
    assert saved.status_code == 201, saved.text
    estimate = client.post("/api/v1/experiments/estimate", json={"config": cfg}).json()
    assert estimate["available"] is False and estimate["low"] is None
    body = {"config_id": saved.json()["id"], "idempotency_key": "repeat-click"}
    run = client.post("/api/v1/experiments/runs", json=body)
    assert run.status_code == 202, run.text
    r = run.json()
    assert r["status"] == "queued" and len(r["cases"]) == 1
    assert client.post("/api/v1/experiments/runs", json=body).json()["id"] == r["id"]
    exp = client.get("/api/v1/experiments/runs/" + r["id"] + "/export")
    assert "never-show" not in exp.text and "credential" not in exp.text
    assert (
        client.post("/api/v1/experiments/runs/" + r["id"] + "/cancel").json()["status"]
        == "cancelled"
    )
    assert client.get("/api/v1/experiments/runs").json()["runs"][0]["finished_cases"] == 1


def test_api_forbids_cross_owner_and_unbounded_pull(client, store):
    foreign = job(store, "other")
    assert client.get("/api/v1/experiments/runs/" + foreign["id"]).status_code == 404
    assert client.post("/api/v1/experiments/runs/" + foreign["id"] + "/cancel").status_code == 404
    assert client.get("/api/v1/experiments/runs/" + foreign["id"] + "/export").status_code == 404
    c = client.post(
        "/api/v1/experiments/connections",
        json={"name": "Local", "kind": "ollama", "base_url": "http://localhost:11434"},
    ).json()
    r = client.post(
        "/api/v1/experiments/local/pull",
        json={"connection_id": c["id"], "model": "arbitrary/remote-model"},
    )
    assert r.status_code == 422
