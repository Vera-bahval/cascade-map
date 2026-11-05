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
    assert r["cases"][0]["scenario_snapshot"]["invoice"]["invoice_prefix"] == "INV"
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


def test_run_billing_uses_saved_tariffs_and_sums_scenarios_without_rewriting_history(client, store):
    conn = store.save_connection(
        '["ns","user"]',
        {"name": "Fixture API", "kind": "chat_completions", "base_url": "https://example.com/v1"},
    )
    cfg = {
        "name": "Tariff snapshot", "connection_id": conn["id"], "model": "fixture-model",
        "scenario_ids": ["baseline_clean", "dirty_data_negative_amount"],
        "input_price": 2, "output_price": 5, "cached_input_price": 0.5, "currency": "RUB",
    }
    saved = client.post("/api/v1/experiments/configs", json=cfg)
    assert saved.status_code == 201, saved.text
    launched = client.post("/api/v1/experiments/runs", json={"config_id": saved.json()["id"]})
    assert launched.status_code == 202, launched.text
    run = launched.json()
    usages = [
        {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": None},
        {"input_tokens": 2000, "output_tokens": 200, "cached_input_tokens": 1000},
    ]
    for case, usage in zip(run["cases"], usages):
        store.append_event(run["id"], {
            "event_type": "llm_call", "case_id": case["id"], "agent_name": "invoice_agent",
            "record": {"success": True, "usage": usage, "response": {"model": "fixture-model"}},
        })
        store.update_case(run["id"], case["id"], status="completed", metrics={"llm_calls": 1})
    store.update_job(run["id"], status="completed", metrics={"llm_calls": 2})
    before = store.get_job(run["id"])
    assert client.post("/api/v1/experiments/configs", json={**cfg, "input_price": 200}).status_code == 201
    detail = client.get("/api/v1/experiments/runs/" + run["id"])
    assert detail.status_code == 200, detail.text
    result = detail.json()
    assert [c["metrics"]["cost"] for c in result["cases"]] == pytest.approx([0.0025, 0.0035])
    assert result["metrics"]["cost"] == pytest.approx(0.006)
    assert result["metrics"]["currency"] == "RUB"
    assert result["metrics"]["cost_basis"] == "tariff_estimate_no_cache_discount"
    assert result["config"]["input_price"] == 2
    history = client.get("/api/v1/experiments/runs").json()["runs"]
    summary = next(item for item in history if item["id"] == run["id"])
    assert summary["metrics"]["cost"] == result["metrics"]["cost"]
    assert summary["metrics"]["cost_basis"] == result["metrics"]["cost_basis"]
    assert "cases" not in summary
    exported = client.get("/api/v1/experiments/runs/" + run["id"] + "/export").json()["run"]
    assert exported["metrics"]["cost"] == result["metrics"]["cost"]
    assert store.get_job(run["id"]) == before


def test_report_and_export_explain_saved_cases_without_mutating_them(client, store):
    own = job(store, '["ns","user"]')
    case_id = own["cases"][0]["id"]
    store.update_case(
        own["id"],
        case_id,
        status="completed",
        result={
            "demonstration": True,
            "agent_chain": [
                {
                    "agent": "invoice_agent",
                    "order": 1,
                    "success": True,
                    "reasoning": "Done",
                    "errors": [],
                }
            ],
        },
    )
    tool = {
        "event_type": "tool_call_success",
        "case_id": case_id,
        "agent_name": "invoice_agent",
        "event_data": {
            "tool_name": "update_invoice_status",
            "tool_output": {"updated": True, "status": "approved"},
        },
    }
    store.append_event(own["id"], tool)
    store.append_event(
        own["id"],
        {"event_type": "llm_call_success", "case_id": case_id, "large_prompt": "excluded"},
    )
    before = store.get_job(own["id"])
    assert len(store.report_events(own["id"])) == 1
    response = client.get("/api/v1/experiments/runs/" + own["id"])
    assert response.status_code == 200
    report = response.json()["cases"][0]["report"]
    assert report["cascade"]["status"] == "not_evaluated"
    assert "одобрен" in report["timeline"][0]["summary"]
    exported = client.get("/api/v1/experiments/runs/" + own["id"] + "/export").json()
    assert report == exported["run"]["cases"][0]["report"]
    assert "secret-never-export" not in response.text
    assert store.get_job(own["id"]) == before


def test_manual_cause_edit_preserves_old_clients_and_exports(client, store):
    own = job(store, '["ns","user"]')
    case_id = own["cases"][0]["id"]
    store.update_case(own["id"], case_id, status="completed")
    url = f"/api/v1/experiments/runs/{own['id']}/cases/{case_id}/assessment"
    response = client.patch(
        url,
        json={
            "type": "full_cascade",
            "reason": "Evidence from the trace",
            "cause_codes": ["unverified_claim"],
        },
    )
    assert response.status_code == 200
    response = client.patch(
        url, json={"type": "half_cascade", "reason": "Updated evidence from trace"}
    )
    assert response.json()["cause_codes"] == ["unverified_claim"]
    exported = client.get(f"/api/v1/experiments/runs/{own['id']}/export").json()
    assert exported["run"]["cases"][0]["assessment"]["cause_codes"] == ["unverified_claim"]
    assert (
        client.patch(
            url,
            json={
                "type": "half_cascade",
                "reason": "Explicit clearing of cause",
                "cause_codes": [],
            },
        ).json()["cause_codes"]
        == []
    )
