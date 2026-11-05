"""One scenario per process: agent instrumentation never crosses experiments."""

import argparse
import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from finbot.experiments.catalog import ROOT, versions
from finbot.experiments.executor import run_case, scenario_catalog, utcnow
from finbot.experiments.store import Store, TERMINAL

STOP_GRACE_SECONDS = 15


async def stop_process(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def run_case_in_process(store, job, case, scenario):
    """Wait for a bounded child process; credentials stay in the encrypted store."""
    if store.cancelled(job["id"]):
        raise asyncio.CancelledError()
    env = {
        **os.environ,
        "DATABASE_TYPE": "sqlite",
        "FINBOT_RESULTS_PG_HOST": "",
    }
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "finbot.experiments.case_process",
            "--db",
            str(Path(store.path).resolve()),
            "--job-id",
            job["id"],
            "--case-id",
            case["id"],
            "--parent-pid",
            str(os.getpid()),
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    process = None
    try:
        process = await asyncio.shield(launch)
        deadline = job["config"].get("scenario_timeout_seconds", 1800) + 60
        await asyncio.wait_for(process.wait(), timeout=deadline)
    except asyncio.CancelledError:
        process = process or await launch
        await stop_process(process)
        raise
    except TimeoutError:
        await stop_process(process)
        store.update_case(
            job["id"],
            case["id"],
            status="failed",
            finished_at=utcnow(),
            error="Scenario process exceeded its time limit; automatic retry disabled.",
        )
        return "failed"
    current = next(c for c in store.get_job(job["id"])["cases"] if c["id"] == case["id"])
    if current["status"] not in TERMINAL:
        store.update_case(
            job["id"],
            case["id"],
            status="failed",
            finished_at=utcnow(),
            error=f"Scenario process exited ({process.returncode}) before saving a result.",
        )
        return "failed"
    return current["status"]


def lease_valid(store, job, parent_pid):
    if parent_pid and os.getppid() != parent_pid:
        return False
    current = store.get_job(job["id"])
    if (
        current["status"] != "running"
        or current.get("cancel_requested")
        or current.get("worker_id") != job.get("worker_id")
    ):
        return False
    with store.db() as db:
        row = db.execute("SELECT seen FROM workers WHERE id=?", (job.get("worker_id"),)).fetchone()
    return row is not None and time.time() - row[0] < 90


async def execute_child(store, job_id, case_id, parent_pid):
    job = store.get_job(job_id, include_secret=True)
    case = next(c for c in job["cases"] if c["id"] == case_id)
    if case["status"] != "queued":
        return
    if not lease_valid(store, job, parent_pid):
        store.update_case(
            job_id,
            case_id,
            status="cancelled" if store.cancelled(job_id) else "interrupted",
            finished_at=utcnow(),
        )
        return
    scenarios, digest = scenario_catalog()
    if (
        job.get("scenario_hash")
        and job["scenario_hash"] != digest
        or job.get("code_revision") not in {None, "unknown", versions()[0]}
    ):
        store.update_case(
            job_id,
            case_id,
            status="failed",
            finished_at=utcnow(),
            error="Code or scenario source changed after queueing; create a new job.",
        )
        return
    scenario = next(s for s in scenarios if s["id"] == case["scenario_id"])
    store.update_case(job_id, case_id, execution={"isolation": "process", "pid": os.getpid()})
    task = asyncio.create_task(run_case(store, job, case, scenario))

    def cancel_once():
        if not task.done() and not task.cancelling():
            task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancel_once)

    async def monitor():
        while not task.done():
            await asyncio.sleep(0.5)
            try:
                valid = lease_valid(store, job, parent_pid)
            except Exception:
                valid = False
            if not valid:
                cancel_once()
                return

    watcher = asyncio.create_task(monitor())
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="finbot-child-") as directory:
        os.environ["DATABASE_TYPE"] = "sqlite"
        os.environ["DATABASE_URL"] = f"sqlite:///{directory}/bootstrap.db"
        asyncio.run(execute_child(Store(args.db), args.job_id, args.case_id, args.parent_pid))


if __name__ == "__main__":
    main()
