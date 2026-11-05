import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from finbot.experiments.catalog import versions
from finbot.experiments.store import Store
from finbot.experiments.worker import run_worker


def enqueue(store, model, count=3, concurrency=2):
    connection = store.save_connection(
        "test", {"name": model, "kind": "mock", "base_url": "", "api_key": ""}
    )
    return store.create_job(
        "test",
        "experiment",
        {"connection_id": connection["id"], "model": model, "scenario_concurrency": concurrency},
        [
            {
                "id": uuid4().hex,
                "scenario_id": "baseline_clean",
                "status": "queued",
                "repeat": i + 1,
            }
            for i in range(count)
        ],
        code_revision=versions()[0],
    )


async def until(predicate, timeout=5):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


def test_atomic_global_limits_across_store_instances(tmp_path):
    store = Store(tmp_path / "queue.db")
    jobs = [enqueue(store, f"model-{i}", count=2) for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(
            pool.map(lambda i: Store(store.path).claim_job(f"w{i}", max_running=2), range(6))
        )
    assert len([job for job in claimed if job]) == 2
    cases = [(job["id"], case["id"]) for job in claimed if job for case in job["cases"]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        acquired = list(
            pool.map(lambda pair: Store(store.path).acquire_case_slot(*pair, limit=3), cases)
        )
    assert sum(acquired) == 3
    selected = next(pair for pair, ok in zip(cases, acquired) if ok)
    assert not store.acquire_case_slot(*selected, limit=4)
    store.release_case_slot(*selected)
    assert store.acquire_case_slot(*selected, limit=3)
    store.cancel("test", selected[0])
    store.release_case_slot(*selected)
    assert not store.acquire_case_slot(*selected, limit=4)


def test_stale_recovery_releases_slots_and_keeps_results(tmp_path):
    store = Store(tmp_path / "queue.db")
    job = enqueue(store, "model")
    store.claim_job("gone")
    assert store.acquire_case_slot(job["id"], job["cases"][0]["id"], 4)
    store.update_case(job["id"], job["cases"][1]["id"], status="completed", result={"kept": True})
    assert store.recover_stale_jobs() == 1
    assert store.worker_status()["active_cases"] == 0
    assert store.get_job(job["id"])["cases"][1]["result"] == {"kept": True}


@pytest.mark.asyncio
async def test_two_models_and_cases_overlap_with_global_cap(tmp_path, monkeypatch):
    store = Store(tmp_path / "queue.db")
    jobs = [enqueue(store, f"model-{i}", concurrency=3) for i in range(3)]
    stop = asyncio.Event()
    gate = asyncio.Event()
    running = set()
    models = set()
    peaks = []

    async def runner(store, job, case, scenario):
        running.add(case["id"])
        models.add(job["config"]["model"])
        peaks.append(len(running))
        store.update_case(job["id"], case["id"], status="running")
        try:
            await gate.wait()
            await asyncio.sleep(0.02)
            store.update_case(
                job["id"], case["id"], status="completed", result={"model": job["config"]["model"]}
            )
            return "completed"
        finally:
            running.remove(case["id"])

    monkeypatch.setattr("finbot.experiments.case_process.run_case_in_process", runner)
    monkeypatch.setattr("finbot.experiments.worker.archive_configured", lambda: False)
    task = asyncio.create_task(
        run_worker(store, stop=stop, poll_seconds=0.01, max_parallel_jobs=2, max_parallel_cases=4)
    )
    try:
        await until(lambda: len(running) == 4)
        assert len(models) == 2
        state = store.worker_status()
        assert state["active_jobs"] == 2
        assert state["active_cases"] == 4
        assert state["max_parallel_jobs"] == 2
        assert state["max_parallel_cases"] == 4
        assert sum(store.get_job(j["id"])["status"] == "queued" for j in jobs) == 1
        gate.set()
        await until(lambda: all(store.get_job(j["id"])["status"] == "completed" for j in jobs))
        assert max(peaks) == 4
        for job in jobs:
            for case in store.get_job(job["id"])["cases"]:
                assert case["result"]["model"] == job["config"]["model"]
    finally:
        gate.set()
        stop.set()
        await asyncio.wait_for(task, 5)
    assert store.worker_status()["active_cases"] == 0


@pytest.mark.asyncio
async def test_cancelling_one_model_does_not_cancel_others(tmp_path, monkeypatch):
    store = Store(tmp_path / "queue.db")
    first, second = [enqueue(store, m, count=2) for m in ("cancel-me", "keep-me")]
    stop = asyncio.Event()
    gate = asyncio.Event()
    started = set()

    async def runner(store, job, case, scenario):
        started.add(job["id"])
        store.update_case(job["id"], case["id"], status="running")
        await gate.wait()
        store.update_case(job["id"], case["id"], status="completed", result={"saved": True})
        return "completed"

    monkeypatch.setattr("finbot.experiments.case_process.run_case_in_process", runner)
    monkeypatch.setattr("finbot.experiments.worker.archive_configured", lambda: False)
    task = asyncio.create_task(
        run_worker(store, stop=stop, poll_seconds=0.01, max_parallel_jobs=2, max_parallel_cases=4)
    )
    try:
        await until(lambda: len(started) == 2)
        store.cancel("test", first["id"])
        await until(lambda: store.get_job(first["id"])["status"] == "cancelled")
        assert store.get_job(second["id"])["status"] == "running"
        gate.set()
        await until(lambda: store.get_job(second["id"])["status"] == "completed")
    finally:
        gate.set()
        stop.set()
        await asyncio.wait_for(task, 5)
    assert all(c["status"] == "cancelled" for c in store.get_job(first["id"])["cases"])
    assert store.worker_status()["active_cases"] == 0


@pytest.mark.asyncio
async def test_shutdown_interrupts_active_without_replaying_queue(tmp_path, monkeypatch):
    store = Store(tmp_path / "queue.db")
    jobs = [enqueue(store, str(i), count=2) for i in range(3)]
    stop = asyncio.Event()
    started = set()

    async def runner(store, job, case, scenario):
        started.add(job["id"])
        store.update_case(job["id"], case["id"], status="running", result={"partial": True})
        await asyncio.Future()

    monkeypatch.setattr("finbot.experiments.case_process.run_case_in_process", runner)
    monkeypatch.setattr("finbot.experiments.worker.archive_configured", lambda: False)
    task = asyncio.create_task(
        run_worker(store, stop=stop, poll_seconds=0.01, max_parallel_jobs=2, max_parallel_cases=4)
    )
    await until(lambda: len(started) == 2)
    stop.set()
    await asyncio.wait_for(task, 5)
    statuses = [store.get_job(j["id"])["status"] for j in jobs]
    assert statuses.count("interrupted") == 2
    assert statuses.count("queued") == 1
    assert store.worker_status()["active_cases"] == 0
    for job in jobs:
        current = store.get_job(job["id"])
        if current["status"] == "interrupted":
            assert all(c["result"]["partial"] for c in current["cases"])


def test_workers_with_different_limits_use_conservative_shared_cap(tmp_path):
    store = Store(tmp_path / "queue.db")
    first = enqueue(store, "one", count=2)
    enqueue(store, "two", count=1)
    store.heartbeat("small", max_jobs=1, max_cases=1)
    store.heartbeat("large", max_jobs=3, max_cases=3)
    assert store.claim_job("large", max_running=3)["id"] == first["id"]
    assert store.claim_job("large", max_running=3) is None
    assert store.acquire_case_slot(first["id"], first["cases"][0]["id"], 3)
    assert not store.acquire_case_slot(first["id"], first["cases"][1]["id"], 3)
    state = store.worker_status()
    assert state["max_parallel_jobs"] == state["max_parallel_cases"] == 1
    assert state["active_jobs"] == state["active_cases"] == 1
