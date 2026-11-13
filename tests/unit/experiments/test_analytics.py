"""Research denominators, missing data, ownership and read-only API contracts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finbot.core.auth.middleware import get_session_context
from finbot.core.auth.session import SessionContext
from finbot.experiments.analytics import (
    AnalyticsFilter,
    calibration,
    fraction,
    selected_runs,
    summarize,
)
from finbot.experiments.analytics_data import local_tables, normalize, read_rows
from finbot.experiments.api import get_owner, get_store, router
from finbot.experiments.store import Store


def iteration(index, confidence, *, status="completed", error=False, span="span"):
    return {
        "source_agent_run_id": span,
        "agent_name": "invoice_agent",
        "iteration_index": index,
        "vanilla_confidence": confidence,
        "lexical_confidence": {"value": confidence},
        "judge": {"status": status, "confidence": confidence, "error_present": error},
    }


def dataset():
    return {
        "source": "fixture",
        "runs": [
            {
                "id": "run-a",
                "model": "A",
                "started_at": "2026-11-01T00:00:00Z",
                "config": {"temperature": 0.7, "seed": 42},
                "metadata": {},
                "is_demo": False,
            },
            {"id": "run-b", "model": "B", "config": {}, "metadata": {}, "is_demo": True},
        ],
        "cases": [
            {
                "id": "c1",
                "run_id": "run-a",
                "scenario_id": "trigger",
                "execution_status": "completed",
                "report": {"detector": {"type": "full_cascade"}, "outcome": {"status": "fail"}},
                "human_review": {"type": "full_cascade"},
                "metrics": {"cost": 2, "currency": "RUB"},
                "observations": {"iterations": [iteration(1, 0.9), iteration(2, 0.2, error=True)]},
            },
            {
                "id": "c2",
                "run_id": "run-a",
                "scenario_id": "trigger",
                "execution_status": "completed",
                "report": {"detector": {"type": "needs_review"}},
                "human_review": {"type": "no_cascade"},
                "metrics": {"cost": 4, "currency": "USD"},
            },
            {
                "id": "c3",
                "run_id": "run-a",
                "scenario_id": "baseline_clean",
                "execution_status": "completed",
                "report": {"detector": {"type": "none"}},
                "human_review": {"type": "no_cascade"},
            },
            {
                "id": "c4",
                "run_id": "run-a",
                "scenario_id": "trigger",
                "execution_status": "failed",
                "report": {"detector": {"type": "full_cascade"}},
                "human_review": {"type": "full_cascade"},
            },
            {
                "id": "c5",
                "run_id": "run-b",
                "scenario_id": "trigger",
                "execution_status": "completed",
                "report": {"detector": {"type": "full_cascade"}},
                "human_review": {"type": "full_cascade"},
            },
        ],
    }


def test_matrix_includes_abstentions_and_excludes_failed_demo_and_missing_truth():
    data = dataset()
    data["cases"].append(
        {
            "id": "unreviewed",
            "run_id": "run-a",
            "scenario_id": "trigger",
            "execution_status": "completed",
            "report": {"detector": {"type": "full_cascade"}},
        }
    )
    result = summarize(data, AnalyticsFilter(include_demo=True))
    assert result["coverage"]["completed"] == 4
    matrix = result["matrix"]
    assert matrix["n"] == 3 and matrix["excluded"] == 1
    assert matrix["accuracy"] == pytest.approx(2 / 3)
    assert matrix["counts"][0][matrix["labels"].index("needs_review")] == 1
    assert result["summary"]["p_casc"]["value"] == 0.5
    assert result["summary"]["p_casc"]["n"] == 2  # baseline and unknown excluded
    assert result["summary"]["cost_by_currency"]["RUB"]["mean"] == 2
    assert result["summary"]["cost_by_currency"]["USD"]["mean"] == 4
    assert result["type_map"]["rows"][0]["model"] == "A"


def test_delta_is_per_invocation_and_never_bridges_missing_judgments():
    data = dataset()
    rows = data["cases"][0]["observations"]["iterations"]
    rows.extend(
        [
            iteration(1, 0.8, span="second"),
            iteration(2, None, span="second"),
            iteration(3, 0.1, span="second"),
        ]
    )
    before = deepcopy(data)
    result = summarize(data, AnalyticsFilter(matrix="vanilla"))
    assert data == before
    assert result["confidence"]["judge"]["delta"]["n"] == 1
    assert result["confidence"]["judge"]["delta"]["mean"] == pytest.approx(0.7)
    assert result["matrix"]["n"] == 4
    assert result["matrix"]["by_model"][0]["model"] == "A"
    assert result["legacy"]["model_confidence"]["n"] == 0


def test_empty_samples_and_calibration_targets_are_explicit():
    assert fraction(0, 0)["value"] is None
    assert fraction(0, 10)["wilson95"][1] > 0
    result = calibration([(False, 1), (True, 0), (False, 0.5), (True, 0.5)], 2)
    assert result["brier"] == 0.125
    assert sum(b["n"] for b in result["bins"]) == 4
    assert calibration([], 10)["ece"] is None
    result = summarize({"runs": [], "cases": [], "source": "empty"}, AnalyticsFilter())
    assert result["matrix"]["accuracy"] is None
    assert result["summary"]["route_deviation"]["n"] == 0


def test_filters_intersect_and_period_is_half_open():
    runs = dataset()["runs"]
    assert (
        len(
            selected_runs(
                runs,
                AnalyticsFilter(
                    models=["A"], config={"temperature": 0.7}, start="2026-11-01T05:00:00+05:00"
                ),
            )
        )
        == 1
    )
    assert not selected_runs(runs, AnalyticsFilter(end="2026-11-01T00:00:00Z"))
    assert not selected_runs(runs, AnalyticsFilter(run_ids=["run-b"], models=["A"]))
    with pytest.raises(ValueError):
        AnalyticsFilter(start="2026-11-02", end="2026-11-01")


def test_route_features_require_ordered_evidence_and_allow_safe_early_stop():
    data = dataset()
    case = data["cases"][0]

    def agent(name, index):
        return {
            "id": name,
            "agent_name": name,
            "step_order": index,
            "provenance": {"identity": "explicit_span"},
        }

    case["agents"] = [agent("invoice_agent", 1)]
    assert summarize(data, AnalyticsFilter())["cases"][0]["route_deviation"] is False
    case["agents"].append(agent("payments_agent", 2))
    assert summarize(data, AnalyticsFilter())["cases"][0]["fraud_skipped"] is True
    case["agents"][0]["provenance"]["identity"] = "agent_chain_only"
    assert summarize(data, AnalyticsFilter())["cases"][0]["route_deviation"] is None


@pytest.fixture
def analytics_client(tmp_path, monkeypatch):
    from finbot.config import settings

    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "FINBOT_RESULTS_PG_HOST", "")
    store = Store(tmp_path / "analytics.db")
    app = FastAPI()
    app.include_router(router)
    now = datetime.now(UTC)
    session = SessionContext("s", "user", True, "ns", now, now + timedelta(days=1))
    app.dependency_overrides[get_owner] = lambda: "own"
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_session_context] = lambda: session
    return TestClient(app), store, session


def create_job(store, owner, model):
    connection = store.save_connection(
        owner,
        {"kind": "mock", "name": model, "base_url": "", "api_key": "test-secret-should-not-leak"},
    )
    return store.create_job(
        owner,
        "experiment",
        {"connection_id": connection["id"], "model": model},
        [{"id": model, "scenario_id": "baseline_clean", "repeat": 1, "status": "completed"}],
    )


def test_local_api_is_owner_scoped_and_tables_expose_no_secrets(analytics_client):
    client, store, _ = analytics_client
    create_job(store, "own", "visible")
    create_job(store, "other", "foreign")
    catalog = client.get("/api/v1/experiments/analytics/catalog").json()
    assert [r["model"] for r in catalog["runs"]] == ["visible"]
    assert len(catalog["tables"]) == 12
    report = client.post("/api/v1/experiments/analytics/query", json={"include_demo": True})
    assert report.status_code == 200, report.text
    assert report.json()["coverage"]["runs"] == 1
    for table in catalog["tables"]:
        response = client.post(
            f'/api/v1/experiments/analytics/tables/{table["table"]}?all_rows=true', json={}
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == table["rows"]
        assert "test-secret-should-not-leak" not in response.text
        assert "foreign" not in response.text
    assert client.post("/api/v1/experiments/analytics/tables/pg_authid", json={}).status_code == 422
    assert (
        client.post("/api/v1/experiments/analytics/tables/cm_runs?limit=101", json={}).status_code
        == 422
    )


def test_local_reader_has_no_200_run_limit(analytics_client):
    _, store, _ = analytics_client
    first = create_job(store, "own", "visible")
    import json

    with store.db() as db:
        data = db.execute("SELECT data FROM jobs WHERE id=?", (first["id"],)).fetchone()[0]
        for index in range(205):
            job = json.loads(data)
            job["id"] = f"extra-{index}"
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (job["id"], "own", "completed", None, None, index, json.dumps(job)),
            )
    assert len(normalize(local_tables(store, "own"), "local")["runs"]) == 206
    assert (
        read_rows(store, "own", AnalyticsFilter(), "cm_runs", 200, 10, all_rows=True)["total"]
        == 206
    )


def test_shared_archive_is_not_exposed_to_production_guests(analytics_client, monkeypatch):
    from finbot.config import settings

    client, _, _ = analytics_client
    monkeypatch.setattr(settings, "DEBUG", False)
    for path in ("query", "interpret", "tables/cm_runs"):
        assert (
            client.post(
                f"/api/v1/experiments/analytics/{path}", json={"source": "archive"}
            ).status_code
            == 403
        )
    assert client.get("/api/v1/experiments/analytics/catalog?source=archive").status_code == 403


def test_unavailable_archive_does_not_silently_fall_back(analytics_client, monkeypatch):
    import finbot.experiments.analytics_data as data

    client, _, _ = analytics_client

    def unavailable(*args, **kwargs):
        raise RuntimeError("password=must-never-be-visible")

    monkeypatch.setattr(data, "archive_read", unavailable)
    response = client.post("/api/v1/experiments/analytics/query", json={"source": "archive"})
    assert response.status_code == 503
    assert "must-never-be-visible" not in response.text


def test_payment_metric_requires_transfer_evidence():
    data = dataset()
    case = data["cases"][0]
    case["agents"] = [
        {
            "id": "payment",
            "agent_name": "payments_agent",
            "step_order": 1,
            "provenance": {"identity": "explicit_span"},
        }
    ]
    report = summarize(data, AnalyticsFilter())
    assert report["summary"]["p_pay"]["value"] is None
    assert report["summary"]["p_reached_payments"]["value"] == 1
    case["result"] = {"payment": {"transfer_count": 0}}
    assert summarize(data, AnalyticsFilter())["summary"]["p_pay"]["value"] == 0
    case["result"]["payment"]["transfer_count"] = 1
    assert summarize(data, AnalyticsFilter())["summary"]["p_pay"]["value"] == 1


def test_interpreter_is_explicit_aggregate_only_and_uses_analysis_schema(
    analytics_client, monkeypatch
):
    import json

    from finbot.core.data.models import LLMResponse
    from finbot.experiments import analytics_data, evaluation, gemini_observer

    client, _, _ = analytics_client
    data = dataset()
    data["cases"][0]["result"] = {"private_trace": "raw-secret-must-not-be-sent"}
    monkeypatch.setattr(analytics_data, "read_dataset", lambda *_: data)
    monkeypatch.setattr(
        evaluation,
        "judge_policy",
        lambda: {
            "kind": "gemini",
            "model": "fixture-gemini",
            "base_url": "https://example.com",
            "generation": {},
        },
    )
    requests = []

    class FakeClient:
        def __init__(self, connection, config, on_call, response_schema):
            assert response_schema["required"] == ["analysis"]
            self.on_call = on_call

        async def chat(self, request):
            requests.append(request)
            self.on_call({"usage": {"input_tokens": 7}})
            return LLMResponse(content=json.dumps({"analysis": "Проверить покрытие разметки."}))

        async def aclose(self):
            pass

    monkeypatch.setattr(gemini_observer, "GeminiObserverClient", FakeClient)
    assert client.post("/api/v1/experiments/analytics/query", json={}).status_code == 200
    assert not requests
    result = client.post("/api/v1/experiments/analytics/interpret", json={})
    assert result.status_code == 200, result.text
    assert result.json()["text"] == "Проверить покрытие разметки."
    assert len(requests) == 1
    assert "raw-secret-must-not-be-sent" not in json.dumps(requests[0].messages)


def test_archive_reader_uses_readonly_snapshot_and_normalizes_child_rows():
    from finbot.experiments.analytics_data import archive_read
    from finbot.experiments.archive_health import TABLES

    run = dataset()["runs"][0]
    case = dataset()["cases"][0]
    observed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, query, params=None):
            text = str(query)
            observed.append(text)
            if "information_schema.columns" in text:
                self.rows = [{"table_name": table, "column_name": "id"} for table in TABLES]
            elif "SELECT * FROM public.cm_runs" in text:
                self.rows = [deepcopy(run)]
            elif "Identifier('cm_cases')" in text:
                self.rows = [deepcopy(case)]
            elif "Identifier('cm_agent_iterations')" in text:
                self.rows = [
                    {
                        "id": "i",
                        "case_id": "c1",
                        "source_agent_run_id": "a",
                        "agent_name": "invoice_agent",
                        "iteration_index": 1,
                        "execution_status": "completed",
                        "vanilla_confidence": 0.9,
                    }
                ]
            elif "Identifier('cm_iteration_evaluations')" in text:
                self.rows = [
                    {
                        "iteration_id": "i",
                        "status": "completed",
                        "confidence": 0.8,
                        "error_present": False,
                        "assessment": {"method": "fixture"},
                    }
                ]
            else:
                self.rows = []

        def fetchall(self):
            return self.rows

    class Connection:
        def set_session(self, **kwargs):
            assert kwargs == {"readonly": True, "isolation_level": "REPEATABLE READ"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def cursor(self, **_):
            return Cursor()

        def close(self):
            pass

    class Archive:
        def connect(self):
            return Connection()

    result = archive_read(AnalyticsFilter(source="archive"), archive=Archive())
    assert len(result["cases"]) == 1
    judge = result["cases"][0]["observations"]["iterations"][0]["judge"]
    assert judge["confidence"] == 0.8 and judge["method"] == "fixture"
    assert all("INSERT" not in statement and "UPDATE " not in statement for statement in observed)
