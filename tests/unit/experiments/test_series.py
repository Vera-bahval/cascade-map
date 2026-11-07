import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finbot.experiments.api import get_owner, get_store, router
from finbot.experiments.series import SeriesManifest, expand_manifest
from finbot.experiments.store import Store


def manifest(**changes):
    return SeriesManifest.model_validate(
        {
            "name": "Sweep",
            "defaults": {"repeats": 5, "scenario_ids": ["baseline_clean"]},
            "matrix": {"temperature": {"start": 0, "stop": 1, "step": 0.1}, "seed": [11, 22]},
            "models": [{"connection_id": "api", "model": "model-a"}],
            **changes,
        }
    )


def expand(value):
    return expand_manifest(
        value, ["baseline_clean"], {"api": "chat_completions", "responses": "responses"}
    )


def test_decimal_endpoints_seeds_and_repeats_are_independent():
    plan = expand(manifest())
    assert plan["run_count"] == 22
    assert plan["attempt_count"] == 110
    assert sorted({r["temperature"] for r in plan["runs"]}) == [n / 10 for n in range(11)]
    assert {r["seed"] for r in plan["runs"]} == {11, 22}
    assert all(r["repeats"] == 5 for r in plan["runs"])


def test_model_overrides_replace_shared_axes_and_null_removes_parameters():
    plan = expand(
        manifest(
            models=[
                {"connection_id": "api", "model": "a"},
                {
                    "connection_id": "responses",
                    "model": "b",
                    "overrides": {"temperature": 0.5, "seed": None},
                },
            ]
        )
    )
    assert plan["run_count"] == 23
    assert plan["runs"][-1]["temperature"] == 0.5
    assert "seed" not in plan["runs"][-1]


@pytest.mark.parametrize(
    "matrix",
    [
        {"top_k": {"start": 0, "stop": 1, "step": 0.2}},
        {"top_p": [0]},
        {"seed": [1, 1]},
        {"temperature": []},
        {"temperature": {"start": 0, "stop": 1, "step": 0.00001}},
        {"unknown": [1]},
    ],
)
def test_invalid_or_unbounded_axes_fail_before_creating_jobs(matrix):
    with pytest.raises(ValueError):
        expand(manifest(matrix=matrix))


def test_unsupported_response_parameters_fail_before_execution():
    with pytest.raises(ValueError, match="Responses"):
        expand(manifest(models=[{"connection_id": "responses", "model": "a"}]))


@pytest.fixture
def api_client(tmp_path):
    store = Store(tmp_path / "series.db")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_owner] = lambda: "owner"
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as client:
        yield client, store


def test_series_api_preview_atomic_launch_idempotency_isolation_and_export(api_client):
    client, store = api_client
    connection = store.save_connection(
        "owner", {"name": "mock", "kind": "mock", "api_key": "secret-example"}
    )
    body = manifest(models=[{"connection_id": connection["id"], "model": "demo"}]).model_dump(
        mode="json"
    )
    preview = client.post("/api/v1/experiments/series/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert store.list_jobs("owner") == []
    config = client.post("/api/v1/experiments/series/configs", json=body).json()
    launch = {"config_id": config["id"], "idempotency_key": "same-series-click"}
    response = client.post("/api/v1/experiments/series", json=launch)
    assert response.status_code == 202, response.text
    series = response.json()
    assert len(series["runs"]) == 22  # A series may exceed the manual queue's 20 jobs.
    assert all(len(store.get_job(r["id"])["cases"]) == 5 for r in series["runs"])
    assert client.post("/api/v1/experiments/series", json=launch).json()["id"] == series["id"]
    assert len(store.list_jobs("owner")) == 22
    assert "secret-example" not in json.dumps(series)
    text = client.get(f'/api/v1/experiments/series/{series["id"]}/export').text
    assert len([json.loads(line) for line in text.splitlines()]) == 23
    assert "secret-example" not in text
    with pytest.raises(KeyError):
        store.get_series(series["id"], "other")
    app = client.app
    app.dependency_overrides[get_owner] = lambda: "other"
    assert client.get(f'/api/v1/experiments/series/{series["id"]}/export').status_code == 404
    assert (
        client.post(f'/api/v1/experiments/series/{series["id"]}/cancel', json={}).status_code == 404
    )
    assert client.post("/api/v1/experiments/series", json=launch).status_code == 404
    app.dependency_overrides[get_owner] = lambda: "owner"
    stopped = client.post(f'/api/v1/experiments/series/{series["id"]}/cancel', json={}).json()
    assert all(r["status"] == "cancelled" for r in stopped["runs"])
    assert stopped["finished_at"] is not None


def test_failed_series_transaction_leaves_no_partial_queue(tmp_path):
    store = Store(tmp_path / "queue.db")
    conn = store.save_connection("owner", {"name": "mock", "kind": "mock"})
    config = {
        "connection_id": conn["id"],
        "model": "demo",
        "scenario_ids": ["baseline_clean"],
        "repeats": 1,
    }
    with pytest.raises(KeyError):
        store.create_series(
            "owner",
            {"name": "broken"},
            {
                "runs": [config, {**config, "connection_id": "missing"}],
                "attempt_count": 2,
            },
            {"baseline_clean": {}},
            key="broken-series",
            revision="test",
            scenario_hash="test",
        )
    assert store.list_jobs("owner") == []
    assert store.list_series("owner") == []


def test_readonly_sqlite_import_preserves_series_manifest_and_expanded_configs(tmp_path):
    from finbot.experiments.archive_import import iter_sqlite_exports
    from finbot.experiments.archive_projection import build_archive

    store = Store(tmp_path / "import-series.db")
    conn = store.save_connection(
        "owner", {"name": "API", "kind": "chat_completions", "api_key": "private-import-key"}
    )
    body = manifest(models=[{"connection_id": conn["id"], "model": "a"}], matrix={"seed": [1]})
    plan = expand_manifest(body, ["baseline_clean"], {conn["id"]: "chat_completions"})
    series = store.create_series(
        "owner",
        body.model_dump(mode="json"),
        plan,
        {"baseline_clean": {}},
        key="import-series-key",
        revision="test",
        scenario_hash="test",
    )
    item = next(iter_sqlite_exports(store.path))
    bundle = build_archive(**item)
    assert bundle["series"]["manifest"] == body.model_dump(mode="json")
    assert bundle["series_runs"][0]["series_id"] == series["id"]
    assert "private-import-key" not in json.dumps(bundle)
