import asyncio
import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from finbot.experiments.case_process import execute_child, run_case_in_process
from finbot.experiments.executor import execute_job, scenario_catalog
from finbot.experiments.store import Store
from tests.unit.experiments.test_executor import MemoryStore, make_job


@pytest.mark.asyncio
async def test_parallel_cases_are_bounded_and_individual_failures_are_preserved():
    job = make_job()
    job["config"]["scenario_concurrency"] = 2
    store = MemoryStore(job)
    active = peak = 0
    first_pair = asyncio.Event()
    seen = []

    async def run(store, job, case, scenario):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            first_pair.set()
        await asyncio.wait_for(first_pair.wait(), 2)
        await asyncio.sleep(0.01)
        active -= 1
        seen.append(case["id"])
        if case["id"] == "1":
            raise ValueError("broken provider")
        store.update_case(job["id"], case["id"], status="completed", result={"saved": True})
        return "completed"

    await execute_job(store, job, run)
    assert peak == 2
    assert len(seen) == len(set(seen)) == 7
    assert store.job["status"] == "failed"
    assert store.job["cases"][1]["status"] == "failed"
    assert all(c["result"]["saved"] for c in store.job["cases"] if c["id"] != "1")


@pytest.mark.asyncio
async def test_cancel_reaps_all_active_cases_and_preserves_completed_result():
    job = make_job()
    job["config"]["scenario_concurrency"] = 2
    store = MemoryStore(job)
    started = asyncio.Event()
    active = set()
    stopped = set()

    async def run(store, job, case, scenario):
        if case["id"] == "0":
            store.update_case(job["id"], case["id"], status="completed", result={"saved": True})
            return "completed"
        active.add(case["id"])
        if len(active) == 2:
            started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.add(case["id"])

    task = asyncio.create_task(execute_job(store, job, run))
    await asyncio.wait_for(started.wait(), 3)
    store.cancel_requested = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == stopped
    assert len(stopped) == 2
    assert store.job["cases"][0]["result"] == {"saved": True}
    assert all(c["status"] == "cancelled" for c in store.job["cases"][1:])


class LocalProvider:
    def __init__(self, expected):
        self.expected = expected
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.active = self.peak = 0
        self.requests = []
        self.handlers = set()

    async def handle(self, reader, writer):
        current = asyncio.current_task()
        self.handlers.add(current)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            payload = json.loads(await reader.readexactly(size))
            self.requests.append(payload)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if len(self.requests) == self.expected:
                self.ready.set()
            await self.release.wait()
            body = json.dumps(
                {
                    "id": "local-scripted",
                    "model": payload["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "complete",
                                        "type": "function",
                                        "function": {
                                            "name": "complete_task",
                                            "arguments": json.dumps(
                                                {
                                                    "task_status": "success",
                                                    "task_summary": payload["model"],
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            self.active -= 1
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(current)

    async def close(self):
        self.release.set()
        await asyncio.gather(*self.handlers, return_exceptions=True)


def queued_job(store, url, model="test-one", concurrency=2, count=2, tariffs=None):
    connection = store.save_connection(
        "test",
        {
            "name": model,
            "kind": "chat_completions",
            "base_url": url,
            "allow_private_network": True,
            "api_key": "test-key-not-for-export",
        },
    )
    scenarios, digest = scenario_catalog()
    store.create_job(
        "test",
        "experiment",
        {
            "connection_id": connection["id"],
            "model": model,
            "scenario_concurrency": concurrency,
            "business_date": "2026-10-24T00:00:00+00:00",
            "timeout_seconds": 60,
            "scenario_timeout_seconds": 90,
            **(tariffs or {}),
        },
        [
            {"id": f"case-{model}-{i}", "scenario_id": scenario["id"], "status": "queued"}
            for i, scenario in enumerate(scenarios[:count])
        ],
        scenario_hash=digest,
    )
    store.heartbeat("test-worker")
    return store.claim_job("test-worker")


@pytest.mark.asyncio
async def test_two_models_and_two_scenarios_overlap_in_separate_processes(tmp_path):
    store = Store(tmp_path / "queue.db")
    provider = LocalProvider(expected=4)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    jobs = [
        queued_job(
            store, f"http://127.0.0.1:{port}/v1", model,
            tariffs={"input_price": price, "output_price": price * 2, "currency": "RUB"},
        )
        for model, price in (("one", 1), ("two", 10))
    ]
    tasks = [asyncio.create_task(execute_job(store, job)) for job in jobs]
    try:
        await asyncio.wait_for(provider.ready.wait(), 45)
        assert provider.peak == 4
        provider.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
        pids, namespaces = set(), set()
        for job in jobs:
            finished = store.get_job(job["id"])
            assert finished["status"] == "completed"
            assert finished["metrics"]["llm_calls"] == 2
            assert finished["metrics"]["input_tokens"] == 20
            assert finished["metrics"]["wall_duration_seconds"] > 0
            expected_cost = 16 * job["config"]["input_price"] / 1_000_000
            assert finished["metrics"]["cost"] == pytest.approx(expected_cost * 2)
            assert finished["metrics"]["currency"] == "RUB"
            for case in finished["cases"]:
                assert case["metrics"]["cost"] == pytest.approx(expected_cost)
                assert case["metrics"]["cost_basis"] == "tariff_estimate"
                pids.add(case["execution"]["pid"])
                namespaces.add(case["namespace"])
                assert case["execution"]["isolation"] == "process"
                assert len(case["result"]["initial_state"]["invoices"]) == 1
                assert len(case["result"]["initial_state"]["vendors"]) == 1
            calls = [
                e for e in store.events(job["id"], limit=1000) if e["event_type"] == "llm_call"
            ]
            assert len(calls) == 2
            assert all(e["record"]["request"]["model"] == job["config"]["model"] for e in calls)
            assert "test-key-not-for-export" not in json.dumps(calls)
        assert len(pids) == len(namespaces) == 4
        assert os.getpid() not in pids
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_process_cancel_or_lost_worker_saves_partial_result(tmp_path, cancel):
    store = Store(tmp_path / "queue.db")
    provider = LocalProvider(expected=1)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    job = queued_job(store, f"http://127.0.0.1:{port}/v1", count=1)
    task = asyncio.create_task(run_case_in_process(store, job, job["cases"][0], {}))
    try:
        await asyncio.wait_for(provider.ready.wait(), 30)
        if cancel:
            store.cancel("test", job["id"])
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 20)
        else:
            with store.db() as db:
                db.execute("UPDATE workers SET seen=0 WHERE id='test-worker'")
            assert await asyncio.wait_for(task, 20) == "interrupted"
        case = store.get_job(job["id"])["cases"][0]
        assert case["status"] == ("cancelled" if cancel else "interrupted")
        assert "final_state" in case["result"]
        assert case["metrics"]["llm_calls"] == 1
        assert case["metrics"]["llm_failures"] == 1
        with pytest.raises(ProcessLookupError):
            os.kill(case["execution"]["pid"], 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_child_rechecks_revision_before_any_model_request(tmp_path):
    store = Store(tmp_path / "queue.db")
    job = queued_job(store, "http://127.0.0.1:1/v1", count=1)
    with patch("finbot.experiments.case_process.versions", return_value=("changed", "")):
        job["code_revision"] = "old"
        with store.db() as db:
            raw = json.loads(
                db.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
            )
            raw["code_revision"] = "old"
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(raw), job["id"]))
        await execute_child(store, job["id"], job["cases"][0]["id"], 0)
    assert store.get_job(job["id"])["cases"][0]["status"] == "failed"
    assert store.events(job["id"]) == []
