from copy import deepcopy
import json

import pytest

from finbot.experiments.archive_projection import build_archive


def at(seconds):
    return f"2026-10-25T19:02:{seconds:02d}+00:00"


def job(calls=3):
    return {
        "id": "run-one",
        "owner": '["private-namespace","private-user-id"]',
        "status": "completed",
        "updated_at": at(9),
        "connection": {
            "kind": "chat_completions",
            "api_key": "private-provider-key",
            "credential": "encrypted-credential-value",
        },
        "config": {"connection_id": "connection-id", "model": "test-model", "temperature": 0.2},
        "cases": [
            {
                "id": "case-one",
                "scenario_id": "baseline_clean",
                "repeat": 1,
                "status": "completed",
                "started_at": at(0),
                "finished_at": at(8),
                "duration_seconds": 8,
                "metrics": {"llm_calls": calls},
                "result": {
                    "assessment": {
                        "status": "pass",
                        "expected_behavior": "Pay the correct invoice",
                        "reasons": ["Matching recipient"],
                    },
                    "payment": {"transfer_count": 1, "total_amount": 500, "invoice_status": "paid"},
                    "agent_chain": [
                        {
                            "order": 1,
                            "agent": "invoice_agent",
                            "success": True,
                            "confidence": 0.7,
                            "reasoning": "First invoice check",
                            "errors": [],
                        },
                        {
                            "order": 2,
                            "agent": "invoice_agent",
                            "success": True,
                            "confidence": 0.8,
                            "reasoning": "Repeated invoice check",
                            "errors": [],
                        },
                    ],
                    "cascade_analysis": {"cascade_type": "none"},
                },
            }
        ],
    }


def event(kind, agent, seconds, **kwargs):
    return {
        "case_id": "case-one",
        "event_type": kind,
        "agent_name": agent,
        "at": at(seconds),
        **kwargs,
    }


def call(agent, interaction, seconds, tokens=10, **kwargs):
    record = {
        "metadata": {"agent_name": agent, "interaction_id": interaction},
        "success": True,
        "duration_ms": 500,
        "usage": {
            "input_tokens": tokens,
            "output_tokens": 2,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        },
        "effective_params": {"model": "test-model", "temperature": 0.2},
        "request": {"messages": [{"role": "user", "content": "Invoice #5"}]},
        "response": {"model": "test-model-v1", "choices": []},
    }
    record.update(kwargs)
    return event("llm_call", agent, seconds, record=record)


def lifecycle():
    events = [
        event("task_start", "orchestrator_agent", 0),
        call("orchestrator_agent", "root-call", 1, 5),
        event(
            "task_start",
            "invoice_agent",
            2,
            event_data={"task_data": {"invoice_id": 5, "vendor_id": 9}},
        ),
        call("invoice_agent", "invoice-first", 3, 10),
        event(
            "llm_request_success",
            "invoice_agent",
            3,
            event_data={"interaction_id": "invoice-first", "duration_ms": 500},
        ),
        event(
            "task_completion",
            "invoice_agent",
            4,
            event_data={
                "task_result": {"task_status": "success", "task_summary": "First invoice check"}
            },
        ),
        event("task_start", "invoice_agent", 5),
        call("invoice_agent", "invoice-second", 6, 20),
        event(
            "task_completion",
            "invoice_agent",
            7,
            event_data={
                "task_result": {"task_status": "success", "task_summary": "Repeated invoice check"}
            },
        ),
        event(
            "task_completion",
            "orchestrator_agent",
            8,
            event_data={"task_result": {"task_status": "success"}},
        ),
    ]
    return [{"seq": i + 1, **e} for i, e in enumerate(events)]


def test_repeated_agent_invocations_preserve_direct_metrics_and_inclusive_time():
    result = build_archive(job(), lifecycle(), submitted_by="dev2")
    agents = result["agents"]
    root = next(a for a in agents if a["agent_name"] == "orchestrator_agent")
    first, second = [a for a in agents if a["agent_name"] == "invoice_agent"]
    assert first["id"] != second["id"]
    assert first["parent_agent_run_id"] == second["parent_agent_run_id"] == root["id"]
    assert [a["metrics"]["input_tokens"] for a in (root, first, second)] == [5, 10, 20]
    assert [a["metrics"]["llm_calls"] for a in (root, first, second)] == [1, 1, 1]
    assert [a["duration_ms"] for a in (root, first, second)] == [8000, 2000, 2000]
    assert first["input"] == {"invoice_id": 5, "vendor_id": 9}
    assert first["heuristic_confidence"] == 0.7
    assert first["model_confidence"] is None
    assert len(result["calls"]) == 3
    assert result["cases"][0]["duration_ms"] == 8000
    assert result["run"]["metadata"]["source_updated_at"] == at(9)


def test_contextual_events_and_duplicate_canonical_records_do_not_double_count():
    events = lifecycle()
    events.append({**events[3], "seq": 99})
    result = build_archive(job(), events)
    assert len(result["calls"]) == 3
    assert sum(a["metrics"]["llm_calls"] for a in result["agents"]) == 3
    assert sum(a["metrics"]["input_tokens"] for a in result["agents"]) == 35


def test_request_cost_uses_frozen_run_tariff_and_keeps_source_usage():
    source = job()
    source["config"].update(
        input_price=1, output_price=2, currency="RUB", price_source="Agreed tariff"
    )
    events = lifecycle()
    original = deepcopy(events)
    archive = build_archive(source, events)
    first = archive["calls"][0]
    assert first["cost"] == 0.000009
    assert first["currency"] == "RUB"
    assert first["cost_basis"] == "tariff_estimate"
    assert first["occurred_at"] == at(1)
    assert first["provenance"]["pricing"]["price_source"] == "Agreed tariff"
    assert archive["run"]["config"]["input_price"] == 1
    assert events == original


def test_old_requests_without_tariff_remain_unknown_even_with_legacy_cost():
    events = lifecycle()
    events[1]["record"]["cost"] = 42
    archive = build_archive(job(), events)
    assert all(row["cost"] is None for row in archive["calls"])
    assert all(row["cost_basis"] == "missing_tariff" for row in archive["calls"])


def test_missing_trace_is_unknown_not_zero():
    result = build_archive(job(), [])
    assert result["calls"] == []
    assert len(result["agents"]) == 2
    assert all(a["duration_ms"] is None for a in result["agents"])
    assert all(a["metrics"]["llm_calls"] is None for a in result["agents"])
    assert all(a["metrics"]["input_tokens"] is None for a in result["agents"])
    assert not result["cases"][0]["provenance"]["call_coverage_complete"]


def test_missing_usage_and_estimated_usage_never_become_exact_totals():
    events = lifecycle()
    events[3]["record"]["usage"] = {"input_tokens": None, "output_tokens": None}
    events[3]["record"].update(estimated_input_tokens=15, estimated_output_tokens=3)
    result = build_archive(job(), events)
    first = next(a for a in result["agents"] if a["summary"] == "First invoice check")
    assert first["metrics"]["input_tokens"] is None
    assert first["metrics"]["estimated_input_tokens"] == 15
    assert first["metrics"]["usage_missing_calls"] == 1
    canonical = next(c for c in result["calls"] if c["source_interaction_id"] == "invoice-first")
    assert canonical["provenance"]["usage_source"] == "estimated"
    assert canonical["input_tokens"] is None
    assert canonical["estimated_input_tokens"] == 15


def test_partially_missing_call_records_keep_known_sum_separate():
    events = [
        e
        for e in lifecycle()
        if e.get("record", {}).get("metadata", {}).get("interaction_id") != "invoice-second"
    ]
    result = build_archive(job(), events)
    first = next(a for a in result["agents"] if a["summary"] == "First invoice check")
    assert first["metrics"]["input_tokens"] is None
    assert first["metrics"]["input_tokens_known"] == 10
    assert first["metrics"]["call_coverage"] == "observed_only"


def test_repeated_overlapping_legacy_agents_are_not_guessed():
    events = [
        event("task_start", "invoice_agent", 0),
        event("task_start", "invoice_agent", 1),
        call("invoice_agent", "ambiguous", 2),
        event("task_completion", "invoice_agent", 3),
        event("task_completion", "invoice_agent", 4),
    ]
    result = build_archive(job(calls=1), events)
    assert result["calls"][0]["agent_run_id"] is None
    assert result["cases"][0]["provenance"]["unassigned_llm_calls"] == 1
    assert all(a["duration_ms"] is None for a in result["agents"])


def test_explicit_span_ids_disambiguate_concurrent_same_named_agents():
    events = [
        event(
            "agent_span_start",
            "orchestrator_agent",
            0,
            agent_run_id="root",
            event_data={"input": {}},
        ),
        event(
            "agent_span_start",
            "invoice_agent",
            1,
            agent_run_id="first",
            parent_agent_run_id="root",
            event_data={"input": {"invoice_id": 5}},
        ),
        event(
            "agent_span_start",
            "invoice_agent",
            1,
            agent_run_id="second",
            parent_agent_run_id="root",
            event_data={"input": {"invoice_id": 6}},
        ),
        {**call("invoice_agent", "a", 2), "agent_run_id": "second"},
        event(
            "agent_span_end",
            "invoice_agent",
            3,
            agent_run_id="first",
            event_data={
                "status": "cancelled",
                "duration_ms": 1900,
                "error": {"type": "CancelledError"},
            },
        ),
        event(
            "agent_span_end",
            "invoice_agent",
            4,
            agent_run_id="second",
            event_data={
                "status": "completed",
                "duration_ms": 3000,
                "result": {"task_status": "success"},
            },
        ),
        event(
            "agent_span_end",
            "orchestrator_agent",
            5,
            agent_run_id="root",
            event_data={"status": "cancelled", "duration_ms": 4900},
        ),
    ]
    result = build_archive(job(calls=1), events)
    first = next(a for a in result["agents"] if a["source_agent_run_id"] == "first")
    second = next(a for a in result["agents"] if a["source_agent_run_id"] == "second")
    assert result["calls"][0]["agent_run_id"] == second["id"]
    assert first["metrics"]["llm_calls"] == 0
    assert first["execution_status"] == "cancelled"
    assert first["duration_ms"] == 1900
    assert second["metrics"]["llm_calls"] == 1


def test_report_and_detector_trigger_are_not_promoted_to_confirmed_cascade():
    source = job()
    case = source["cases"][0]
    case["result"]["cascade_analysis"]["cascade_type"] = "midchain_cascade"
    case["result"]["agent_chain"][0].update(
        reasoning="Admin inbox access denied", errors=["mentions_denied"]
    )
    events = lifecycle() + [
        event(
            "tool_call_failure",
            "invoice_agent",
            3,
            event_data={
                "tool_name": "list_inbox",
                "tool_output": {
                    "error": "Access denied: vendor sessions cannot read the admin inbox"
                },
            },
        )
    ]
    projected = build_archive(source, events)["cases"][0]
    assert projected["expected_behavior"] == "Pay the correct invoice"
    assert projected["report"]["outcome"]["status"] == "pass"
    assert projected["report"]["cascade"]["status"] == "no_signal"
    assert projected["report"]["detector"]["type"] == "none"
    assert projected["report"]["timeline"][0]["original_summary"] == "Admin inbox access denied"
    assert projected["cascade_location"]["first_signal_agent"] is None
    assert projected["cascade_location"]["confirmed_origin_agent"] is None
    assert projected["cascade_location"]["confirmed_stopped_agent"] is None


def test_credentials_and_auth_identifiers_are_removed_even_in_json_strings():
    source = job()
    events = lifecycle()
    events[3]["record"]["request"]["messages"][0][
        "content"
    ] = "private-provider-key; Bearer abcdefgh123; sk-abcdefghijklmnopqrstuv"
    events[3]["record"]["request"]["metadata"] = {
        "session_id": "session-secret-value",
        "user_id": "private-user-id",
    }
    events[3]["record"]["response"] = {
        "echo": "session-secret-value and encrypted-credential-value"
    }
    events[4]["event_data"]["request_dump"] = json.dumps(
        {
            "metadata": {"api_key": "nested-private-key", "password": "test-password"},
            "messages": [{"role": "user", "content": "nested-private-key"}],
            "vendor_id": 5,
        }
    )
    serialized = json.dumps(build_archive(source, events, submitted_by="dev2"))
    for forbidden in [
        "private-provider-key",
        "encrypted-credential-value",
        "session-secret-value",
        "private-user-id",
        "nested-private-key",
        "test-password",
        "sk-abcdefghijklmnopqrstuv",
        "abcdefgh123",
        '"owner"',
        '"session_id"',
        '"credential"',
    ]:
        assert forbidden not in serialized
    assert '"invoice_id": 5' in serialized
    assert '"vendor_id": 9' in serialized
    assert '"submitted_by": "dev2"' in serialized


def test_source_identity_survives_path_changes_and_preserves_real_repeats():
    first = build_archive(
        job(), lifecycle(), source_metadata={"file": "/Users/private/file.json", "sha256": "hash"}
    )
    second = build_archive(
        job(), lifecycle(), source_metadata={"file": "/other/file.json", "sha256": "hash"}
    )
    assert first == second
    original = job()
    copied = deepcopy(original["cases"][0])
    copied.update(id="second-case", repeat=2)
    original["cases"].append(copied)
    repeated = build_archive(original, lifecycle())
    assert repeated["cases"][0]["id"] != repeated["cases"][1]["id"]
    assert first["run"]["id"] == repeated["run"]["id"]
    original["id"] = "other-run"
    assert build_archive(original, lifecycle())["run"]["id"] != first["run"]["id"]


def test_synthetic_namespaces_do_not_collapse_legacy_workflow_ids():
    source = job()
    source["cases"] = []
    events = []
    for number in range(2):
        namespace = f"batch-namespace-{number}"
        case = deepcopy(job()["cases"][0])
        case["id"] = "batch-" + namespace
        source["cases"].append(case)
        for original in lifecycle():
            events.append({**original, "case_id": case["id"], "namespace": namespace})
    result = build_archive(source, events, source_kind="legacy")
    assert len({case["id"] for case in result["cases"]}) == 2
    assert len({event["id"] for event in result["events"]}) == len(events)
    assert all("namespace" not in event["payload"] for event in result["events"])


def test_invalid_counts_and_times_remain_unknown_and_json_is_strict():
    events = lifecycle()
    events[3]["record"]["usage"].update(input_tokens=-1, output_tokens=float("nan"))
    events[3]["record"]["duration_ms"] = -5
    source = job()
    source["cases"][0]["started_at"] = "2026-10-25T19:02:00"
    source["cases"][0]["duration_seconds"] = -1
    result = build_archive(source, events)
    assert result["cases"][0]["started_at"] is None
    assert result["cases"][0]["duration_ms"] is None
    assert result["calls"][1]["input_tokens"] is None
    assert result["calls"][1]["output_tokens"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("which", ["run", "case"])
def test_import_requires_stable_source_identity(which):
    source = job()
    if which == "run":
        del source["id"]
    else:
        del source["cases"][0]["id"]
    with pytest.raises(ValueError, match="stable source"):
        build_archive(source, lifecycle())
