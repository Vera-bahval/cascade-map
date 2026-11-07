import asyncio
import copy
import json
from collections import Counter
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from finbot.experiments.agent_spans import run_in_span, span_fields
from finbot.experiments.executor import instrument_agents, run_case, scenario_catalog


@pytest.mark.asyncio
async def test_nested_repeated_delegates_have_distinct_identity_and_restore_parent():
    events, calls = [], []

    async def capture(**event):
        events.append(event)

    async def child():
        calls.append(span_fields())
        await asyncio.sleep(0)
        return {"task_status": "success", "task_summary": "Child finished"}

    async def root():
        root_identity = span_fields()
        for _ in range(2):
            await run_in_span(capture, "invoice_agent", {}, child)
            assert span_fields() == root_identity
        return {"task_status": "success"}

    await run_in_span(capture, "orchestrator_agent", {}, root)
    starts = [e for e in events if e["event_type"] == "agent_span_start"]
    ends = [e for e in events if e["event_type"] == "agent_span_end"]
    assert len({e["agent_run_id"] for e in starts}) == 3
    assert {e["agent_run_id"] for e in starts} == {e["agent_run_id"] for e in ends}
    assert starts[0]["parent_agent_run_id"] is None
    assert all(e["parent_agent_run_id"] == starts[0]["agent_run_id"] for e in starts[1:])
    assert all(c["parent_agent_run_id"] == starts[0]["agent_run_id"] for c in calls)
    assert all(e["event_data"]["duration_ms"] >= 0 for e in ends)
    assert ends[-1]["agent_run_id"] == starts[0]["agent_run_id"]
    assert span_fields() == {}


@pytest.mark.asyncio
async def test_instrumentation_spans_delegate_failure_before_lifecycle_event():
    from finbot.agents import runner

    events = []

    async def capture(**event):
        events.append(event)

    async def broken_runner(**kwargs):
        assert kwargs["max_retries"] == 1
        raise RuntimeError("Setup failed")

    with patch.object(runner, "run_agent_with_retry", broken_runner):
        with instrument_agents(capture, datetime.now(UTC), 2):
            with pytest.raises(RuntimeError, match="Setup failed"):
                await runner.run_agent_with_retry(runner.InvoiceAgent, None, {"invoice_id": 1})
    assert [e["event_type"] for e in events] == ["agent_span_start", "agent_span_end"]
    assert events[0]["agent_name"] == "invoice_agent"
    assert events[0]["event_data"]["input"] == {"invoice_id": 1}
    assert events[-1]["event_data"]["status"] == "failed"
    assert events[-1]["event_data"]["error"]["type"] == "RuntimeError"
    assert span_fields() == {}


@pytest.mark.asyncio
async def test_span_failure_result_is_preserved_without_raising():
    events = []

    async def capture(**event):
        events.append(event)

    async def invoke():
        return {"task_status": "failed", "task_summary": "Agent could not finish"}

    result = await run_in_span(capture, "invoice_agent", {}, invoke)
    assert events[-1]["event_data"]["status"] == "failed"
    assert events[-1]["event_data"]["result"] == result


class MemoryStore:
    def __init__(self, job):
        self.job = copy.deepcopy(job)
        self.cancel_requested = False
        self.events = []

    def cancelled(self, job_id):
        return self.cancel_requested

    def append_event(self, job_id, event):
        self.events.append(event)

    def update_case(self, job_id, case_id, **fields):
        self.job["cases"][0].update(fields)


def job_fixture():
    return {
        "id": "span-test",
        "created_at": "2026-10-24T00:00:00+00:00",
        "config": {"model": "span-test", "max_iterations": 2},
        "connection": {"kind": "mock"},
        "cases": [{"id": "span-case", "scenario_id": "baseline_clean", "status": "queued"}],
    }


@pytest.mark.asyncio
async def test_cancel_keeps_end_span_and_llm_identity_even_when_cancel_flag_is_set():
    started = asyncio.Event()

    class WaitingClient:
        provider = "mock"
        default_model = "span-test"
        default_temperature = None

        def __init__(self, connection, config, on_call):
            self.on_call = on_call

        async def chat(self, request):
            started.set()
            try:
                await asyncio.Future()
            finally:
                await self.on_call(
                    {
                        "success": False,
                        "metadata": request.metadata,
                        "error": "Request cancelled",
                        "duration_ms": 1,
                    }
                )

        async def aclose(self):
            pass

    job = job_fixture()
    store = MemoryStore(job)
    with patch("finbot.experiments.providers.ExperimentLLMClient", WaitingClient):
        task = asyncio.create_task(run_case(store, job, job["cases"][0], scenario_catalog()[0][0]))
        await asyncio.wait_for(started.wait(), 10)
        store.cancel_requested = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    start = next(e for e in store.events if e["event_type"] == "agent_span_start")
    end = next(e for e in store.events if e["event_type"] == "agent_span_end")
    call = next(e for e in store.events if e["event_type"] == "llm_call")
    assert start["agent_run_id"] == end["agent_run_id"] == call["agent_run_id"]
    assert call["record"]["metadata"]["agent_run_id"] == start["agent_run_id"]
    assert call["record"]["metadata"]["agent_name"] == "orchestrator_agent"
    assert end["event_data"]["status"] == "cancelled"
    assert store.job["cases"][0]["status"] == "cancelled"
    assert all(e.get("agent_run_id") for e in store.events)
    assert span_fields() == {}


@pytest.mark.asyncio
async def test_root_constructor_failure_is_recorded_before_any_task_event():
    job = job_fixture()
    store = MemoryStore(job)
    with patch(
        "finbot.agents.cascade.CascadeOrchestratorAgent.__init__",
        side_effect=ValueError("Setup failed"),
    ):
        status = await run_case(store, job, job["cases"][0], scenario_catalog()[0][0])
    assert status == "failed"
    assert [e["event_type"] for e in store.events] == ["agent_span_start", "agent_span_end"]
    assert store.events[-1]["event_data"]["status"] == "failed"
    assert store.events[-1]["event_data"]["error"]["type"] == "ValueError"


@pytest.mark.asyncio
async def test_real_repeated_delegations_link_each_llm_call_to_its_own_invocation():
    from finbot.core.data.models import LLMResponse

    class ScriptedClient:
        provider = "mock"
        default_model = "span-test"
        default_temperature = None

        def __init__(self, connection, config, on_call):
            self.on_call = on_call
            self.counts = Counter()

        async def chat(self, request):
            agent_name = request.metadata["agent_name"]
            self.counts[agent_name] += 1
            name, args = "complete_task", {"task_status": "success", "task_summary": "Finished"}
            if agent_name == "orchestrator_agent" and self.counts[agent_name] <= 2:
                name = "delegate_to_invoice"
                args = {"invoice_id": 1, "task_description": "Review invoice"}
            await self.on_call(
                {
                    "success": True,
                    "metadata": request.metadata,
                    "duration_ms": 1,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            )
            call_id = f"{agent_name}-{self.counts[agent_name]}"
            return LLMResponse(
                content="",
                provider="mock",
                success=True,
                tool_calls=[{"name": name, "arguments": args, "call_id": call_id}],
                messages=list(request.messages)
                + [
                    {
                        "type": "function_call",
                        "name": name,
                        "arguments": json.dumps(args),
                        "call_id": call_id,
                    }
                ],
            )

        async def aclose(self):
            pass

    job = job_fixture()
    job["config"]["max_iterations"] = 5
    store = MemoryStore(job)
    with patch("finbot.experiments.providers.ExperimentLLMClient", ScriptedClient):
        await run_case(store, job, job["cases"][0], scenario_catalog()[0][0])
    starts = [e for e in store.events if e["event_type"] == "agent_span_start"]
    ends = [e for e in store.events if e["event_type"] == "agent_span_end"]
    assert len(starts) == len(ends) == 3
    root, first, second = starts
    assert first["agent_name"] == second["agent_name"] == "invoice_agent"
    assert first["agent_run_id"] != second["agent_run_id"]
    assert first["parent_agent_run_id"] == second["parent_agent_run_id"] == root["agent_run_id"]
    calls = [e for e in store.events if e["event_type"] == "llm_call"]
    assert Counter(e["agent_run_id"] for e in calls) == {
        root["agent_run_id"]: 3,
        first["agent_run_id"]: 1,
        second["agent_run_id"]: 1,
    }
    assert all(e["agent_run_id"] == e["record"]["metadata"]["agent_run_id"] for e in calls)
    assert all(e["event_data"]["status"] == "completed" for e in ends)
    iterations = store.job["cases"][0]["observations"]["iterations"]
    assert len(iterations) == 5
    assert [
        r["iteration_index"] for r in iterations if r["source_agent_run_id"] == root["agent_run_id"]
    ] == [1, 2, 3]
    assert all(r["status"] == "completed" for r in iterations)
    assert all(len(r["calls"]) == 1 for r in iterations)
    assert span_fields() == {}
