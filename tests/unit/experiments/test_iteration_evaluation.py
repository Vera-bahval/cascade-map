import copy
import json
from types import SimpleNamespace

import pytest

from finbot.config import settings
from finbot.experiments.evaluation import evaluate_once
from finbot.experiments.store import Store


@pytest.fixture
def evaluation_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FINBOT_JUDGE_MODEL", "fixed-observer")
    monkeypatch.setattr(settings, "FINBOT_JUDGE_KIND", "chat_completions")
    monkeypatch.setattr(settings, "FINBOT_JUDGE_BASE_URL", "https://observer.example/v1")
    monkeypatch.setattr(settings, "FINBOT_JUDGE_API_KEY", "private-observer-key")
    store = Store(tmp_path / "observer.db")
    conn = store.save_connection(
        "owner",
        {"name": "test", "kind": "chat_completions", "base_url": "https://studied.example/v1"},
    )
    iterations = [
        {
            "source_agent_run_id": "invoice-invocation",
            "agent_name": "invoice_agent",
            "iteration_index": index,
            "status": "completed",
            "tool_events": [],
            "calls": [
                {
                    "request": {
                        "messages": [
                            {"role": "user", "content": f"prefix through iteration {index}"}
                        ]
                    },
                    "observation": {"content": "Observed answer"},
                }
            ],
            "judge": {"status": "not_requested", "confidence": None},
        }
        for index in [1, 2]
    ]
    job = store.create_job(
        "owner",
        "experiment",
        {"connection_id": conn["id"], "model": "studied", "evaluate_iterations": True},
        [
            {
                "id": "case",
                "scenario_id": "baseline_clean",
                "status": "completed",
                "result": {"agent_result": {"task_summary": "original answer"}},
                "observations": {"vanilla": {"final_confidence": 0.42}, "iterations": iterations},
            }
        ],
    )
    store.update_job(job["id"], status="completed")
    return store, job["id"]


@pytest.mark.asyncio
async def test_observer_uses_fixed_model_and_prefixes_without_changing_studied_outcomes(
    evaluation_job, monkeypatch
):
    store, ident = evaluation_job
    before = copy.deepcopy(store.get_job(ident))
    requests = []

    class Client:
        def __init__(self, connection, config, on_call):
            assert config["model"] == "fixed-observer"
            assert connection["base_url"] == "https://observer.example/v1"
            self.on_call = on_call

        async def chat(self, request):
            requests.append(request)
            self.on_call(
                {
                    "success": True,
                    "usage": {"input_tokens": 10},
                    "response": {"model": "fixed-observer-v1"},
                    "error": "private-observer-key",
                }
            )
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "confidence": 0.9 if len(requests) == 1 else 0.6,
                        "error_present": None,
                        "reason": "Evidence-based estimate",
                        "evidence": [],
                    }
                )
            )

        async def aclose(self):
            pass

    monkeypatch.setattr("finbot.experiments.providers.ExperimentLLMClient", Client)
    assert await evaluate_once(store)
    after = store.get_job(ident)
    assert after["cases"][0]["result"] == before["cases"][0]["result"]
    observation = after["cases"][0]["observations"]
    assert observation["vanilla"] == before["cases"][0]["observations"]["vanilla"]
    assert observation["confidence_profiles"][0]["confidence_degradation"] == 0.3
    assert "private-observer-key" not in json.dumps(observation)
    first_context = json.loads(requests[0].messages[1]["content"])
    assert "prefix through iteration 2" not in json.dumps(first_context)
    assert store.evaluation_status(ident)["status"] == "completed"
    assert not await evaluate_once(store)


@pytest.mark.asyncio
async def test_observer_errors_require_explicit_retry_and_successful_rows_are_not_replayed(
    evaluation_job, monkeypatch
):
    store, ident = evaluation_job
    calls = []

    class Client:
        def __init__(self, connection, config, on_call):
            pass

        async def chat(self, request):
            calls.append(request)
            if len(calls) == 1:
                return SimpleNamespace(content="invalid JSON")
            return SimpleNamespace(
                content='{"confidence":0.8,"error_present":null,"reason":"Evidence","evidence":[]}'
            )

        async def aclose(self):
            pass

    monkeypatch.setattr("finbot.experiments.providers.ExperimentLLMClient", Client)
    await evaluate_once(store)
    assert store.evaluation_status(ident)["status"] == "error"
    assert len(calls) == 2
    assert not await evaluate_once(store)
    store.retry_evaluation("owner", ident)
    await evaluate_once(store)
    assert len(calls) == 3
    assert store.evaluation_status(ident)["status"] == "completed"


@pytest.mark.asyncio
async def test_changed_observer_is_rejected_without_calls(evaluation_job, monkeypatch):
    store, ident = evaluation_job
    monkeypatch.setattr(settings, "FINBOT_JUDGE_MODEL", "other-observer")
    await evaluate_once(store)
    assert store.evaluation_status(ident)["status"] == "error"
    assert all(
        r["judge"]["status"] == "not_requested"
        for r in store.get_job(ident)["cases"][0]["observations"]["iterations"]
    )


def test_expired_evaluation_is_interrupted_not_replayed(evaluation_job):
    store, ident = evaluation_job
    delivery = store.claim_evaluation()
    with store.db() as db:
        db.execute("UPDATE evaluations SET lease_until=0")
    assert store.claim_evaluation() is None
    assert store.evaluation_status(ident)["status"] == "interrupted"
    store.retry_evaluation("owner", ident)
    newer = store.claim_evaluation()
    store.finish_evaluation(delivery)
    assert store.evaluation_status(ident)["status"] == "running"
    store.finish_evaluation(newer)
    assert store.evaluation_status(ident)["status"] == "completed"


@pytest.mark.asyncio
async def test_gemini_observer_uses_openai_key_environment_variable(
    evaluation_job, tmp_path, monkeypatch
):
    source, ident = evaluation_job
    monkeypatch.setattr(settings, "FINBOT_JUDGE_KIND", "gemini")
    monkeypatch.setattr(settings, "FINBOT_JUDGE_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(
        settings,
        "FINBOT_JUDGE_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
    )
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "google-key-from-openai-variable")
    store = Store(tmp_path / "native-gemini.db")
    connection = store.save_connection("owner", {"name": "studied", "kind": "chat_completions"})
    job = store.create_job(
        "owner",
        "experiment",
        {"connection_id": connection["id"], "model": "studied", "evaluate_iterations": True},
        source.get_job(ident)["cases"],
    )
    store.update_job(job["id"], status="completed")

    class Client:
        def __init__(self, connection, config, on_call):
            assert connection["api_key"] == "google-key-from-openai-variable"
            assert connection["api_key"] != settings.FINBOT_JUDGE_API_KEY

        async def chat(self, request):
            return SimpleNamespace(
                content='{"confidence":0.8,"error_present":null,"reason":"Evidence","evidence":[]}'
            )

        async def aclose(self):
            pass

    monkeypatch.setattr("finbot.experiments.gemini_observer.GeminiObserverClient", Client)
    await evaluate_once(store)
    assert store.evaluation_status(job["id"])["status"] == "completed"
