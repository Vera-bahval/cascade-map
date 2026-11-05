import json

import pytest

from finbot.experiments.archive import sync_once
from finbot.experiments.store import Store


def finished_job(store, *, kind="chat_completions", owner="owner"):
    connection = store.save_connection(
        owner,
        {
            "name": "test",
            "kind": kind,
            "base_url": "https://provider.example/v1",
            "api_key": "private-test-token",
        },
    )
    job = store.create_job(
        owner,
        "experiment",
        {"model": "test", "connection_id": connection["id"]},
        [
            {
                "id": "case",
                "scenario_id": "baseline_clean",
                "repeat": 1,
                "status": "completed",
                "metrics": {"llm_calls": 0},
            }
        ],
    )
    store.update_job(job["id"], status="completed")
    return job["id"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "experiments.db")


def test_result_is_queued_in_same_local_transaction_and_mock_is_excluded(store):
    real = finished_job(store)
    mock = finished_job(store, kind="mock")
    states = store.result_sync_statuses()
    assert states[real]["status"] == "pending"
    assert mock not in states
    assert store.get_job(real)["status"] == "completed"


def test_stale_delivery_cannot_acknowledge_new_manual_review(store):
    ident = finished_job(store)
    old = store.claim_result_sync()
    store.update_case(ident, "case", assessment={"type": "no_cascade", "reason": "Reviewed"})
    store.finish_result_sync(old)
    assert store.result_sync_statuses()[ident]["status"] == "pending"
    new = store.claim_result_sync()
    assert new["revision"] > old["revision"]
    store.finish_result_sync(new)
    assert store.result_sync_statuses()[ident]["status"] == "synced"


def test_failed_delivery_retries_without_changing_model_execution(store):
    ident = finished_job(store)
    before = store.get_job(ident)

    class Archive:
        parameters = {"user": "dev2"}
        failing = True
        delivered = []

        def save(self, bundle):
            if self.failing:
                raise RuntimeError("private-test-token")
            self.delivered.append(bundle)

    archive = Archive()
    assert sync_once(store, archive)
    state = store.result_sync_statuses()[ident]
    assert state["status"] == "error"
    assert "private-test-token" not in json.dumps(state)
    assert store.get_job(ident) == before
    assert not sync_once(store, archive)
    store.request_result_sync(ident)
    archive.failing = False
    assert sync_once(store, archive)
    assert store.result_sync_statuses()[ident]["status"] == "synced"
    assert len(archive.delivered) == 1
    assert "private-test-token" not in json.dumps(archive.delivered)
    assert store.get_job(ident) == before


def test_expired_delivery_lease_can_be_reclaimed(store, monkeypatch):
    from finbot.experiments import store as module

    ident = finished_job(store)
    monkeypatch.setattr(module.time, "time", lambda: 100)
    first = store.claim_result_sync(lease_seconds=10)
    assert store.claim_result_sync() is None
    monkeypatch.setattr(module.time, "time", lambda: 111)
    second = store.claim_result_sync()
    assert second["job_id"] == first["job_id"]
    assert second["lease_id"] != first["lease_id"]
    store.finish_result_sync(first)
    assert store.result_sync_statuses()[ident]["status"] == "uploading"
    store.finish_result_sync(second)
    assert store.result_sync_statuses()[ident]["status"] == "synced"


def test_local_only_configuration_does_not_claim_pending_deliveries(store, monkeypatch):
    from finbot.config import settings

    ident = finished_job(store)
    monkeypatch.setattr(settings, "FINBOT_RESULTS_PG_HOST", "")
    assert not sync_once(store)
    assert store.result_sync_statuses()[ident]["status"] == "pending"


def test_sync_api_respects_owner_and_never_requeues_model(store, monkeypatch):
    from datetime import datetime, UTC, timedelta
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from finbot.config import settings
    from finbot.core.auth.session import SessionContext
    from finbot.core.auth.middleware import get_session_context
    from finbot.experiments.api import router, get_store, owner

    now = datetime.now(UTC)
    ctx = SessionContext("session", "user", False, "ns", now, now + timedelta(days=1))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_context] = lambda: ctx
    app.dependency_overrides[get_store] = lambda: store
    client = TestClient(app)
    own = finished_job(store, owner=owner(ctx))
    foreign = finished_job(store, owner="another-user")
    monkeypatch.setattr(settings, "FINBOT_RESULTS_PG_HOST", "")
    assert client.post(f"/api/v1/experiments/runs/{own}/sync").status_code == 409
    monkeypatch.setattr(settings, "FINBOT_RESULTS_PG_HOST", "configured.example")
    assert client.post(f"/api/v1/experiments/runs/{foreign}/sync").status_code == 404
    assert client.post(f"/api/v1/experiments/runs/{own}/sync").status_code == 202
    response = client.get("/api/v1/experiments/runs").json()
    assert len(response["runs"]) == 1
    assert response["runs"][0]["result_sync"]["status"] == "pending"
    assert store.get_job(own)["status"] == "completed"
