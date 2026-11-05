"""Bounded parallel queue worker; interrupted requests are never retried automatically."""

import argparse
import asyncio
import logging
import signal
import time
from uuid import uuid4

from finbot.experiments.executor import execute_job, safe_json, utcnow
from finbot.experiments.catalog import versions
from finbot.experiments.store import Store
from finbot.experiments.archive import configured as archive_configured, sync_once

logger = logging.getLogger(__name__)


async def execute_pull(store, job):
    from finbot.experiments.providers import pull_model

    async def progress(data):
        store.update_job(job["id"], progress=data)
        store.append_event(
            job["id"], {"at": utcnow(), "event_type": "model_pull", "progress": data}
        )
        if store.cancelled(job["id"]):
            raise asyncio.CancelledError()

    try:
        result = await pull_model(job["connection"], job["config"]["model"], on_progress=progress)
        store.update_job(job["id"], status="completed", result=result, finished_at=utcnow())
    except asyncio.CancelledError:
        store.update_job(
            job["id"],
            status="cancelled" if store.cancelled(job["id"]) else "interrupted",
            finished_at=utcnow(),
        )
        raise


async def run_worker(
    store=None,
    *,
    once=False,
    poll_seconds=2,
    stop=None,
    max_parallel_jobs=None,
    max_parallel_cases=None,
):
    from finbot.config import settings
    from finbot.experiments.case_process import run_case_in_process

    store = store or Store()
    stop = stop or asyncio.Event()
    max_jobs = (
        max_parallel_jobs
        if max_parallel_jobs is not None
        else settings.FINBOT_EXPERIMENTS_MAX_PARALLEL_JOBS
    )
    max_cases = (
        max_parallel_cases
        if max_parallel_cases is not None
        else settings.FINBOT_EXPERIMENTS_MAX_PARALLEL_CASES
    )
    if not 1 <= max_jobs <= 8 or not 1 <= max_cases <= 16:
        raise ValueError("Parallel job/case limits are outside supported bounds")
    worker_id = f"worker-{uuid4().hex}"
    worker_revision = versions()[0]
    active = {}
    slots = asyncio.Semaphore(max_cases)
    claimed = 0

    def heartbeat():
        store.heartbeat(worker_id, max_jobs=max_jobs, max_cases=max_cases)

    heartbeat()
    store.recover_stale_jobs()

    async def wait_or_stop(seconds):
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def isolated_case(store, job, case, scenario):
        async with slots:
            while not store.acquire_case_slot(job["id"], case["id"], max_cases):
                current = store.get_job(job["id"])
                if (
                    stop.is_set()
                    or current.get("cancel_requested")
                    or current["status"] != "running"
                ):
                    raise asyncio.CancelledError()
                if next(c for c in current["cases"] if c["id"] == case["id"])["status"] != "queued":
                    raise RuntimeError("Case was already started; automatic replay is disabled")
                await asyncio.sleep(0.2)
            try:
                return await run_case_in_process(store, job, case, scenario)
            finally:
                store.release_case_slot(job["id"], case["id"])

    async def execute(job):
        try:
            if job["kind"] == "experiment" and (
                versions()[0] != worker_revision
                or job.get("code_revision") not in {None, "unknown", worker_revision}
            ):
                raise RuntimeError(
                    "Worker source changed or differs from queued snapshot. Restart worker and create a new job."
                )
            capacity = store.worker_status()
            effective_jobs = min(max_jobs, capacity.get("max_parallel_jobs") or max_jobs)
            effective_cases = min(max_cases, capacity.get("max_parallel_cases") or max_cases)
            execution = {
                "max_parallel_jobs": effective_jobs,
                "max_parallel_cases": effective_cases,
                "scenario_concurrency": min(
                    job["config"].get("scenario_concurrency", 1), effective_cases
                ),
                "isolation": "process_per_scenario",
            }
            store.update_job(job["id"], execution=execution)
            if job["kind"] == "model_pull":
                await execute_pull(store, job)
            else:
                await execute_job(store, job, case_runner=isolated_case)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = safe_json(
                {"type": type(exc).__name__, "message": str(exc)},
                [job["connection"].get("api_key", "")],
            )
            for case in store.get_job(job["id"])["cases"]:
                if case["status"] in {"queued", "running"}:
                    store.update_case(
                        job["id"], case["id"], status="failed", error=error, finished_at=utcnow()
                    )
            store.update_job(job["id"], status="failed", error=error, finished_at=utcnow())
            logger.error("Experiment job %s failed: %s", job["id"], type(exc).__name__)

    async def maintain():
        last_heartbeat = 0
        while not stop.is_set():
            if time.monotonic() - last_heartbeat >= 10:
                heartbeat()
                store.recover_stale_jobs()
                last_heartbeat = time.monotonic()
            for ident, task in list(active.items()):
                if not task.done() and not task.cancelling() and store.cancelled(ident):
                    task.cancel()
            await wait_or_stop(0.2)

    async def archive_results():
        while not stop.is_set():
            if archive_configured():
                try:
                    await asyncio.to_thread(sync_once, store)
                except Exception as exc:
                    logger.warning("Result archive sync failed: %s", type(exc).__name__)
            await wait_or_stop(poll_seconds)

    maintenance = asyncio.create_task(maintain())
    archive = asyncio.create_task(archive_results())
    try:
        while not stop.is_set():
            for ident, task in list(active.items()):
                if task.done():
                    await asyncio.gather(task, return_exceptions=True)
                    del active[ident]
            if once and claimed and not active:
                break
            while len(active) < max_jobs and not (once and claimed) and not stop.is_set():
                job = store.claim_job(worker_id, max_running=max_jobs)
                if job is None:
                    break
                claimed += 1
                active[job["id"]] = asyncio.create_task(execute(job))
            if once and not active:
                break
            await wait_or_stop(min(poll_seconds, 0.2) if active else poll_seconds)
    finally:
        stop.set()
        for task in active.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        # Let an in-flight archive transaction finish before another sync begins.
        await asyncio.gather(archive, return_exceptions=True)
        store.finish_interrupted(worker_id)
        if once and archive_configured():
            await asyncio.to_thread(sync_once, store)


async def main(once=False):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_worker(once=once, stop=stop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Process at most one queued job, then exit"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(args.once))
