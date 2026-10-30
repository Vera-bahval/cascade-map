"""Serial queue worker. Interrupted requests are never retried automatically."""

import argparse
import asyncio
import logging
import signal
import time
from uuid import uuid4

from finbot.experiments.executor import execute_job, safe_json, utcnow
from finbot.experiments.catalog import versions
from finbot.experiments.store import Store

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


async def run_worker(store=None, *, once=False, poll_seconds=2, stop=None):
    store = store or Store()
    stop = stop or asyncio.Event()
    worker_id = f"worker-{uuid4().hex}"
    worker_revision = versions()[0]
    active = None
    store.heartbeat(worker_id)
    store.recover_stale_jobs()

    async def maintain():
        last_heartbeat = 0
        while not stop.is_set():
            if time.monotonic() - last_heartbeat >= 10:
                store.heartbeat(worker_id)
                store.recover_stale_jobs()
                last_heartbeat = time.monotonic()
            if active and not active[1].cancelling() and store.cancelled(active[0]):
                active[1].cancel()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass
        if active:
            active[1].cancel()

    maintenance = asyncio.create_task(maintain())
    try:
        while not stop.is_set():
            job = store.claim_job(worker_id)
            if job is None:
                if once:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
                continue
            if job["kind"] == "experiment" and (
                versions()[0] != worker_revision
                or job.get("code_revision") not in {None, "unknown", worker_revision}
            ):
                error = "Worker source changed or differs from queued snapshot. Restart worker and create a new job."
                for case in job["cases"]:
                    if case["status"] in {"queued", "running"}:
                        store.update_case(
                            job["id"],
                            case["id"],
                            status="failed",
                            error=error,
                            finished_at=utcnow(),
                        )
                store.update_job(job["id"], status="failed", error=error, finished_at=utcnow())
                if once:
                    break
                continue
            coroutine = (
                execute_pull(store, job) if job["kind"] == "model_pull" else execute_job(store, job)
            )
            active = (job["id"], asyncio.create_task(coroutine))
            try:
                await active[1]
            except asyncio.CancelledError:
                # A user cancel stops the current job, not the worker queue.
                if not store.cancelled(job["id"]):
                    stop.set()
            except Exception as exc:
                error = safe_json(
                    {"type": type(exc).__name__, "message": str(exc)},
                    [job["connection"].get("api_key", "")],
                )
                for case in store.get_job(job["id"])["cases"]:
                    if case["status"] in {"queued", "running"}:
                        store.update_case(
                            job["id"],
                            case["id"],
                            status="failed",
                            error=error,
                            finished_at=utcnow(),
                        )
                store.update_job(job["id"], status="failed", error=error, finished_at=utcnow())
                logger.error("Experiment job %s failed: %s", job["id"], type(exc).__name__)
            finally:
                active = None
            if once:
                break
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        if active:
            active[1].cancel()
            await asyncio.gather(active[1], return_exceptions=True)
        store.finish_interrupted(worker_id)


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
