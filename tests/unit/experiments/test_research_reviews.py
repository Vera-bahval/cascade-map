from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finbot.core.auth.middleware import get_session_context
from finbot.core.auth.session import SessionContext
from finbot.experiments.analytics_data import local_tables, normalize
from finbot.experiments.api import get_owner, get_store, router
from finbot.experiments.store import Store


@pytest.fixture
def client_store(tmp_path):
    store = Store(tmp_path / "reviews.db")
    conn = store.save_connection("owner", {"name": "test", "kind": "mock"})
    job = store.create_job(
        "owner",
        "experiment",
        {"connection_id": conn["id"], "model": "mock"},
        [
            {
                "id": "case",
                "scenario_id": "baseline_clean",
                "status": "completed",
                "result": {"payment": {"transfer_count": 0}},
                "observations": {
                    "iterations": [
                        {
                            "source_agent_run_id": "span",
                            "agent_name": "invoice_agent",
                            "iteration_index": 1,
                            "status": "completed",
                            "calls": [],
                            "tool_events": [],
                            "judge": {"status": "not_requested"},
                        }
                    ]
                },
            }
        ],
    )
    app = FastAPI()
    app.include_router(router)
    now = datetime.now(UTC)
    session = SessionContext("session", "user-a", False, "ns", now, now + timedelta(days=1))
    app.dependency_overrides[get_owner] = lambda: "owner"
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_session_context] = lambda: session
    with TestClient(app) as client:
        yield client, store, job, session


def body(**extra):
    return {"type": "no_cascade", "reason": "Verified against saved trace and state", **extra}


def test_review_history_is_atomic_revisioned_and_preserves_original_results(client_store):
    client, store, job, session = client_store
    original = store.get_job(job["id"])["cases"][0]["result"]
    path = f'/api/v1/experiments/runs/{job["id"]}/cases/case/assessment'
    first = client.patch(
        path,
        json=body(
            expected_revision=0,
            agent_errors=[
                {
                    "agent_ref": "span",
                    "iteration_index": 1,
                    "error_present": False,
                    "error_origin": "none",
                    "reason": "The saved step is supported by the evidence",
                }
            ],
        ),
    )
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert client.patch(path, json=body(expected_revision=0)).status_code == 422
    assert len(store.get_job(job["id"])["cases"][0]["assessment_history"]) == 1
    session.user_id = "user-b"
    second = client.patch(path, json=body(expected_revision=1))
    assert second.status_code == 200, second.text
    case = store.get_job(job["id"])["cases"][0]
    assert len(case["assessment_history"]) == 2
    assert (
        "agent_errors" not in case["assessment"]
    )  # A second rater does not inherit another's labels.
    assert case["result"] == original
    assert (
        case["assessment_history"][0]["reviewer_key"]
        != case["assessment_history"][1]["reviewer_key"]
    )


def test_analytics_review_reaches_local_case_but_not_other_owner(client_store):
    client, store, job, session = client_store
    normalized = normalize(local_tables(store, "owner"), "local")["cases"][0]
    path = f'/api/v1/experiments/analytics/cases/{normalized["id"]}'
    result = client.get(path)
    assert result.status_code == 200 and result.json()["review_targets"]
    saved = client.post(path + "/assessment", json=body(expected_revision=0))
    assert saved.status_code == 201, saved.text
    client.app.dependency_overrides[get_owner] = lambda: "other"
    assert client.get(path).status_code == 404
    assert client.post(path + "/assessment", json=body()).status_code == 404


def test_review_rejects_foreign_invocations_and_archive_writes_require_access(
    client_store, monkeypatch
):
    client, store, job, session = client_store
    path = f'/api/v1/experiments/runs/{job["id"]}/cases/case/assessment'
    response = client.patch(
        path,
        json=body(
            agent_errors=[{"agent_ref": "foreign", "reason": "Evidence belongs to another case"}]
        ),
    )
    assert response.status_code == 422
    assert "assessment_history" not in store.get_job(job["id"])["cases"][0]
    normalized = normalize(local_tables(store, "owner"), "local")["cases"][0]
    monkeypatch.setattr("finbot.experiments.api.archive_analytics_allowed", lambda _: False)
    path = f'/api/v1/experiments/analytics/cases/{normalized["id"]}'
    assert client.get(path + "?source=archive").status_code == 403
    assert client.post(path + "/assessment?source=archive", json=body()).status_code == 403
