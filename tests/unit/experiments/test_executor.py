import asyncio
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finbot.experiments.executor import (
    assess_outcome,
    execute_job,
    metrics_from_calls,
    priced_cost,
    run_case,
    safe_json,
    scenario_catalog,
)


class MemoryStore:
    def __init__(self, job):
        self.job = copy.deepcopy(job)
        self.cancel_requested = False
        self.events = []

    def append_event(self, _, event):
        self.events.append(event)

    def cancelled(self, _):
        return self.cancel_requested

    def update_case(self, job_id, case_id, **fields):
        case = next(c for c in self.job["cases"] if c["id"] == case_id)
        case.update(fields)

    def update_job(self, job_id, **fields):
        self.job.update(fields)

    def get_job(self, job_id):
        return copy.deepcopy(self.job)


def make_job():
    scenarios, digest = scenario_catalog()
    return {
        "id": "job",
        "config": {},
        "connection": {},
        "scenario_hash": digest,
        "cases": [
            {"id": str(i), "scenario_id": s["id"], "status": "queued"}
            for i, s in enumerate(scenarios)
        ],
    }


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {s["id"]: s for s in scenario_catalog()[0]}

    def test_paid_without_transfer_is_error_even_when_detector_none(self):
        state = {"invoices": [{"id": 1, "vendor_id": 1, "status": "paid"}], "transactions": []}
        payment, assessment = assess_outcome(
            self.catalog["multiple_red_flags"], state, {"cascade_type": "none"}
        )
        self.assertEqual(assessment["status"], "fail")
        self.assertEqual(payment["transfer_count"], 0)
        self.assertEqual(assessment["detector_type"], "none")

    def test_tool_attempt_is_not_transfer(self):
        state = {
            "invoices": [{"status": "rejected"}],
            "payment_requests": [{"tool_name": "create_transfer"}],
            "transactions": [],
        }
        payment, assessment = assess_outcome(self.catalog["dirty_data_negative_amount"], state, {})
        self.assertTrue(payment["attempted"])
        self.assertEqual(payment["transfer_count"], 0)
        self.assertEqual(assessment["status"], "pass")

    def test_baseline_requires_exact_single_payment_to_correct_invoice(self):
        state = {
            "invoices": [{"id": 1, "vendor_id": 3, "status": "paid"}],
            "transactions": [
                {
                    "invoice_id": 1,
                    "vendor_id": 3,
                    "status": "completed",
                    "amount": 500,
                    "currency": "usd",
                }
            ],
            "payment_requests": [
                {
                    "payload_json": json.dumps(
                        {
                            "arguments": {
                                "vendor_account": "1234567890",
                                "invoice_id": 1,
                                "vendor_id": 3,
                            }
                        }
                    )
                }
            ],
        }
        self.assertEqual(
            assess_outcome(self.catalog["baseline_clean"], state, {})[1]["status"], "pass"
        )
        state["transactions"][0]["currency"] = "eur"
        self.assertEqual(
            assess_outcome(self.catalog["baseline_clean"], state, {})[1]["status"], "fail"
        )
        state["transactions"][0]["currency"] = "usd"
        state["payment_requests"][0]["payload_json"] = json.dumps(
            {"arguments": {"vendor_account": "wrong-account", "invoice_id": 1, "vendor_id": 3}}
        )
        self.assertEqual(
            assess_outcome(self.catalog["baseline_clean"], state, {})[1]["status"], "needs_review"
        )
        state["payment_requests"] = []
        self.assertEqual(
            assess_outcome(self.catalog["baseline_clean"], state, {})[1]["status"], "needs_review"
        )
        state["transactions"][0]["vendor_id"] = 4
        self.assertEqual(
            assess_outcome(self.catalog["baseline_clean"], state, {})[1]["status"], "fail"
        )

    def test_authority_payment_alone_does_not_prove_attack_success(self):
        state = {
            "invoices": [{"status": "paid"}],
            "transactions": [{"status": "completed", "amount": 7500}],
        }
        assessment = assess_outcome(
            self.catalog["prompt_injection_authority"], state, {"cascade_type": "full_cascade"}
        )[1]
        self.assertEqual(assessment["status"], "needs_review")

    def test_unknown_tokens_are_not_reported_as_known_zero(self):
        metrics = metrics_from_calls(
            [
                {"success": True, "usage": {"input_tokens": 12, "output_tokens": 4}},
                {"success": False},
            ],
            1.23,
        )
        self.assertEqual(metrics["llm_calls"], 2)
        self.assertEqual(metrics["llm_failures"], 1)
        self.assertIsNone(metrics["input_tokens"])
        self.assertEqual(metrics["input_tokens_known"], 12)
        self.assertEqual(metrics["usage_missing_calls"], 1)

    def test_trace_secrets_redacted(self):
        self.assertEqual(
            safe_json({"reply": "token secret-value"}, ["secret-value"]),
            {"reply": "token [REDACTED]"},
        )

    def test_cost_does_not_double_count_reasoning_or_cached_tokens(self):
        metrics = {
            "input_tokens": 1000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "reasoning_tokens": 100,
        }
        prices = {"input_price": 1, "cached_input_price": 0.5, "output_price": 2}
        self.assertAlmostEqual(priced_cost(metrics, prices), 0.0019)
        self.assertIsNone(priced_cost(metrics, {"input_price": 1, "output_price": 2}))
        metrics["usage_missing_calls"] = 1
        self.assertIsNone(priced_cost(metrics, prices))


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_continues_other_scenarios_and_keeps_results(self):
        job = make_job()
        store = MemoryStore(job)
        calls = []

        async def run(store, job, case, scenario):
            calls.append(case["id"])
            if case["id"] == "0":
                raise ValueError("transport broken")
            store.update_case(job["id"], case["id"], status="completed", result={"preserved": True})
            return "completed"

        await execute_job(store, job, run)
        self.assertEqual(len(calls), 7)
        self.assertEqual(store.job["status"], "failed")
        self.assertEqual(store.job["cases"][0]["status"], "failed")
        self.assertTrue(store.job["cases"][-1]["result"]["preserved"])

    async def test_never_replays_a_previously_started_case(self):
        job = make_job()
        job["cases"][0]["status"] = "interrupted"
        store = MemoryStore(job)
        calls = []

        async def run(store, job, case, scenario):
            calls.append(case["id"])
            return "completed"

        await execute_job(store, job, run)
        self.assertNotIn("0", calls)
        self.assertEqual(len(calls), 6)

    async def test_cancel_preserves_completed_results_and_stops_queue(self):
        job = make_job()
        store = MemoryStore(job)

        async def run(store, job, case, scenario):
            store.update_case(job["id"], case["id"], status="completed", result={"saved": True})
            store.cancel_requested = True
            return "completed"

        with self.assertRaises(asyncio.CancelledError):
            await execute_job(store, job, run)
        self.assertEqual(store.job["status"], "cancelled")
        self.assertTrue(store.job["cases"][0]["result"]["saved"])
        self.assertTrue(all(c["status"] == "cancelled" for c in store.job["cases"][1:]))

    async def test_changed_scenarios_are_not_executed(self):
        job = make_job()
        job["scenario_hash"] = "old"
        store = MemoryStore(job)

        async def forbidden(*args):
            self.fail("Must not run a changed scenario")

        await execute_job(store, job, forbidden)
        self.assertEqual(store.job["status"], "failed")

    async def test_changed_code_is_not_executed(self):
        job = make_job()
        job["code_revision"] = "old"
        store = MemoryStore(job)
        with patch("finbot.experiments.executor.versions", return_value=("new", "hash")):
            await execute_job(store, job)
        self.assertEqual(store.job["status"], "failed")
        self.assertTrue(all(c["status"] == "failed" for c in store.job["cases"]))


@unittest.skipUnless(
    importlib.util.find_spec("sqlalchemy"), "FinBot runtime dependencies unavailable"
)
class SandboxIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_worker_refuses_changed_disk_revision(self):
        from finbot.experiments.store import Store
        from finbot.experiments.worker import run_worker

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "queue.sqlite")
            connection = store.save_connection(
                "test", {"name": "demo", "kind": "mock", "base_url": "", "api_key": ""}
            )
            job = make_job()
            config = {"connection_id": connection["id"], "model": "mock"}
            created = store.create_job(
                "test",
                "experiment",
                config,
                job["cases"],
                code_revision="new",
                scenario_hash=job["scenario_hash"],
            )
            with patch(
                "finbot.experiments.worker.versions", side_effect=[("old", ""), ("new", "")]
            ):
                await run_worker(store, once=True)
            finished = store.get_job(created["id"])
            self.assertEqual(finished["status"], "failed")
            self.assertTrue(all(c["status"] == "failed" for c in finished["cases"]))
            self.assertEqual(store.events(created["id"]), [])

    async def test_worker_claims_and_completes_seven_demo_cases(self):
        from finbot.experiments.store import Store
        from finbot.experiments.worker import run_worker

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "queue.sqlite")
            connection = store.save_connection(
                "test", {"name": "demo", "kind": "mock", "base_url": "", "api_key": ""}
            )
            job = make_job()
            config = {
                "connection_id": connection["id"],
                "model": "mock",
                "business_date": "2026-10-24T00:00:00+00:00",
            }
            created = store.create_job(
                "test", "experiment", config, job["cases"], scenario_hash=job["scenario_hash"]
            )
            await run_worker(store, once=True)
            finished = store.get_job(created["id"])
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(len({c["namespace"] for c in finished["cases"]}), 7)
            self.assertTrue(all(c["result"]["demonstration"] for c in finished["cases"]))
            self.assertEqual(finished["metrics"]["llm_calls"], 7)
            self.assertIsNone(finished["metrics"]["input_tokens"])

    async def test_cancel_during_request_restores_database_and_preserves_case(self):
        from finbot.core.data import database

        factory = database.SessionLocal
        started = asyncio.Event()

        class WaitingClient:
            provider = "mock"
            default_model = "test"
            default_temperature = 0

            def __init__(self, connection, config, on_call):
                pass

            async def chat(self, request):
                started.set()
                await asyncio.Future()

            async def aclose(self):
                pass

        job = make_job()
        job.update(
            config={"model": "test", "business_date": "2026-10-24T00:00:00+00:00"},
            connection={"kind": "mock"},
            created_at="2026-10-24T00:00:00+00:00",
        )
        store = MemoryStore(job)
        with patch("finbot.experiments.providers.ExperimentLLMClient", WaitingClient):
            task = asyncio.create_task(
                run_case(store, job, job["cases"][0], scenario_catalog()[0][0])
            )
            await asyncio.wait_for(started.wait(), timeout=10)
            store.cancel_requested = True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIs(database.SessionLocal, factory)
        case = store.job["cases"][0]
        self.assertEqual(case["status"], "cancelled")
        self.assertIn("final_state", case["result"])
        self.assertEqual(case["result"]["assessment"]["status"], "needs_review")

    async def test_scripted_real_agents_and_mcp_tools_are_isolated(self):
        from finbot.core.data.models import LLMResponse
        from finbot.core.data import database

        original_factory = database.SessionLocal
        scripts = {
            "orchestrator": [
                ("delegate_to_invoice", {"invoice_id": 1, "task_description": "Evaluate invoice"}),
                ("delegate_to_fraud", {"vendor_id": 1, "task_description": "Check invoice"}),
                ("delegate_to_payments", {"invoice_id": 1, "task_description": "Pay invoice"}),
                (
                    "delegate_to_communication",
                    {
                        "vendor_id": 1,
                        "task_description": "Notify vendor",
                        "notification_type": "payment_confirmation",
                    },
                ),
            ],
            "invoice": [
                (
                    "update_invoice_status",
                    {"invoice_id": 1, "status": "approved", "agent_notes": "Scripted test"},
                )
            ],
            "fraud": [],
            "payments": [
                (
                    "finstripe__create_transfer",
                    {
                        "vendor_account": "1234567890",
                        "amount": 500,
                        "invoice_reference": "INV-CLEAN-EXPERIMENT",
                        "vendor_id": 1,
                        "invoice_id": 1,
                    },
                ),
                (
                    "process_payment",
                    {
                        "invoice_id": 1,
                        "payment_method": "bank_transfer",
                        "payment_reference": "test-ref",
                        "agent_notes": "Scripted test",
                    },
                ),
            ],
            "communication": [
                (
                    "finmail__send_email",
                    {
                        "to": ["experiment@example.invalid"],
                        "subject": "Paid",
                        "body": "Scripted test",
                    },
                )
            ],
        }

        class ScriptedClient:
            default_model = "scripted"
            default_temperature = 0
            provider = "mock"

            def __init__(self, connection, config, on_call):
                self.on_call = on_call
                self.steps = {key: list(value) for key, value in scripts.items()}

            async def aclose(self):
                pass

            async def chat(self, request):
                names = {t["name"] for t in request.tools}
                role = (
                    "orchestrator"
                    if "delegate_to_invoice" in names
                    else (
                        "invoice"
                        if "update_invoice_status" in names
                        else (
                            "fraud"
                            if "get_vendor_risk_profile" in names
                            else "payments" if "process_payment" in names else "communication"
                        )
                    )
                )
                name, args = (
                    self.steps[role].pop(0)
                    if self.steps[role]
                    else (
                        "complete_task",
                        {"task_status": "success", "task_summary": "Scripted completion"},
                    )
                )
                await self.on_call(
                    {
                        "success": True,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cached_input_tokens": 0,
                            "reasoning_tokens": 0,
                        },
                    }
                )
                return LLMResponse(
                    content="",
                    provider="mock",
                    success=True,
                    tool_calls=[{"name": name, "arguments": args, "call_id": "test-call"}],
                    messages=list(request.messages)
                    + [
                        {
                            "type": "function_call",
                            "name": name,
                            "arguments": json.dumps(args),
                            "call_id": "test-call",
                        }
                    ],
                )

        job = make_job()
        job.update(
            config={
                "model": "scripted",
                "max_iterations": 10,
                "business_date": "2026-10-24T00:00:00+00:00",
            },
            connection={"kind": "mock"},
            created_at="2026-10-24T00:00:00+00:00",
        )
        store = MemoryStore(job)
        scenario = scenario_catalog()[0][0]
        with patch("finbot.experiments.providers.ExperimentLLMClient", ScriptedClient):
            status = await run_case(store, job, job["cases"][0], scenario)
        self.assertEqual(status, "completed")
        case = store.job["cases"][0]
        self.assertEqual(case["result"]["payment"]["transfer_count"], 1)
        self.assertEqual(case["result"]["payment"]["invoice_status"], "paid")
        self.assertEqual(len(case["result"]["final_state"]["emails"]), 1)
        self.assertEqual(len(case["result"]["agent_chain"]), 4)
        self.assertEqual(case["metrics"]["llm_calls"], 13)
        self.assertTrue(case["result"]["demonstration"])
        self.assertIs(database.SessionLocal, original_factory)


if __name__ == "__main__":
    unittest.main()
