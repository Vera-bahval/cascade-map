import copy
import hashlib
import json
import sqlite3

import pytest

from finbot.experiments.archive_import import iter_legacy_exports, iter_sqlite_exports


def legacy_case(workflow="wf-one", *, provider_usage=True):
    request = {"messages": [{"role": "user", "content": "Check invoice"}]}
    response = {
        "success": True,
        "content": "Done",
        "metadata": {"model": "returned-model", "effective_parameters": {"max_tokens": 5000}},
    }
    if provider_usage:
        response["metadata"]["usage"] = {"input_tokens": 12, "output_tokens": 3}
    events = [
        {
            "at": "2026-10-25T10:00:00Z",
            "agent_name": "invoice_agent",
            "event_type": "task_start",
            "event_data": {},
        },
        {
            "at": "2026-10-25T10:00:01Z",
            "agent_name": "invoice_agent",
            "event_type": "llm_request_start",
            "session_context": {"user_id": "private-user"},
            "event_data": {
                "interaction_id": "call-one",
                "model": "requested-model",
                "request_dump": json.dumps(request),
            },
        },
        {
            "at": "2026-10-25T10:00:02Z",
            "agent_name": "invoice_agent",
            "event_type": "llm_request_success",
            "event_data": {
                "interaction_id": "call-one",
                "success": True,
                "duration_ms": 1000,
                "response_dump": json.dumps(response),
            },
        },
        {
            "at": "2026-10-25T10:00:03Z",
            "agent_name": "invoice_agent",
            "event_type": "task_completion",
            "event_data": {"task_result": {"task_status": "success", "task_summary": "Approved"}},
        },
    ]
    return {
        "workflow_id": workflow,
        "scenario": {
            "id": "baseline_clean",
            "invoice": {"amount": 500},
            "expected_cascade_type": "none",
        },
        "started_at": "2026-10-25T10:00:00Z",
        "finished_at": "2026-10-25T10:00:03Z",
        "duration_seconds": 3,
        "result": {"task_status": "success", "task_summary": "Paid"},
        "agent_chain": [
            {
                "order": 1,
                "agent": "invoice_agent",
                "success": True,
                "confidence": 0.7,
                "reasoning": "Approved",
            }
        ],
        "cascade_analysis": {"cascade_type": "midchain_cascade"},
        "final_state": {
            "invoices": [{"id": 1, "vendor_id": 2, "status": "paid"}],
            "transactions": [
                {
                    "invoice_id": 1,
                    "vendor_id": 2,
                    "status": "completed",
                    "currency": "usd",
                    "amount": 500,
                }
            ],
            "payment_requests": [
                {
                    "payload_json": json.dumps(
                        {
                            "arguments": {
                                "invoice_id": 1,
                                "vendor_id": 2,
                                "vendor_account": "1234567890",
                                "currency": "usd",
                            }
                        }
                    )
                }
            ],
        },
        "events": events,
    }


def write_batch(root, name="batch", *, run="run-one", cases=None, metrics=True, estimated=False):
    directory = root / name
    directory.mkdir(parents=True)
    cases = cases if cases is not None else [legacy_case(provider_usage=not estimated)]
    data = {
        "run_id": run,
        "provider": "dslab",
        "model": "requested-model",
        "configured_temperature": 1.0,
        "temperature": None,
        "temperature_policy": "omitted",
        "api_style": "anthropic_messages",
        "results": cases,
    }
    (directory / "results.json").write_text(json.dumps(data))
    if metrics:
        rows = []
        for case in cases:
            call = {
                "interaction_id": "call-one",
                "agent": "invoice_agent",
                "duration_seconds": 1.0,
                "temperature": 1.0,
                "effective_parameters": {"max_tokens": 5000},
                "model": "requested-model",
                "actual_model": "returned-model",
            }
            if estimated:
                call.update(estimated_input_tokens=15, estimated_output_tokens=4)
            else:
                call["provider_usage"] = {"input_tokens": 12, "output_tokens": 3}
            rows.append(
                {"scenario_id": case["scenario"]["id"], "llm_calls_started": 1, "calls": [call]}
            )
        (directory / "metrics.json").write_text(json.dumps({"results": rows}))
    return directory


@pytest.mark.parametrize("latest_source", ["run", "case", "event"])
def test_legacy_update_time_uses_latest_saved_evidence(tmp_path, latest_source):
    case = legacy_case()
    if latest_source == "case":
        case["finished_at"] = "2026-10-25T13:00:05+03:00"
    elif latest_source == "event":
        case["events"][-1]["at"] = "2026-10-25T10:00:05Z"
    directory = write_batch(tmp_path, cases=[case])
    path = directory / "results.json"
    data = json.loads(path.read_text())
    data["finished_at"] = (
        "2026-10-25T10:00:05Z" if latest_source == "run" else "2026-10-25T10:00:02Z"
    )
    path.write_text(json.dumps(data))
    exported = next(iter_legacy_exports(tmp_path))
    assert exported["job"]["updated_at"] == "2026-10-25T10:00:05Z"
    assert exported["source_metadata"]["updated_at_source"] == "latest_evidence_timestamp"


def test_legacy_missing_update_time_is_not_import_or_file_time(tmp_path):
    case = legacy_case()
    case["finished_at"] = "not-a-timestamp"
    for event in case["events"]:
        event["at"] = "2026-10-25T10:00:03"  # No timezone evidence.
    write_batch(tmp_path, cases=[case])
    exported = next(iter_legacy_exports(tmp_path))
    assert exported["job"]["updated_at"] is None
    assert exported["source_metadata"]["updated_at_source"] == "unavailable"


@pytest.mark.parametrize("estimated", [False, True])
def test_legacy_preserves_usage_source_and_actual_parameters(tmp_path, estimated):
    write_batch(tmp_path, estimated=estimated)
    exported = next(iter_legacy_exports(tmp_path))
    job = exported["job"]
    assert job["config"]["configured_temperature"] == 1.0
    assert job["config"]["temperature"] is None
    calls = [e for e in exported["events"] if e["event_type"] == "llm_call"]
    assert len(calls) == 1
    record = calls[0]["record"]
    assert "temperature" not in record["effective_params"]
    assert record["metadata"]["model_returned"] == "returned-model"
    assert record["duration_ms"] == 1000
    assert record["usage"]["input_tokens"] == (None if estimated else 12)
    assert record["estimated_input_tokens"] == (15 if estimated else None)
    assert record["provenance"]["usage_source"] == ("estimated" if estimated else "provider")
    assert all("session_context" not in e for e in exported["events"])
    lifecycle_end = next(e for e in exported["events"] if e["event_type"] == "task_completion")
    assert calls[0]["seq"] < lifecycle_end["seq"]
    case = job["cases"][0]
    assert case["result"]["assessment"]["status"] == "pass"
    assert case["result"]["cascade_analysis"]["cascade_type"] == "midchain_cascade"
    assert case["provenance"]["assessment_basis"] == "recomputed_from_saved_state"


def test_import_deduplicates_copies_but_keeps_real_repetitions(tmp_path):
    first = legacy_case("wf-one")
    second = legacy_case("wf-two")
    write_batch(tmp_path, "a", cases=[first, second])
    write_batch(tmp_path, "copy", cases=[first, second])
    write_batch(tmp_path, "separate-run", run="run-two", cases=[first])
    write_batch(tmp_path, "prepared/staging", run="ignored")
    write_batch(tmp_path, "diagnostics/check", run="ignored-too")
    write_batch(tmp_path, "smoke-case", run="ignored-three")
    (tmp_path / "a" / "01-case.json").write_text(json.dumps(first))
    exports = list(iter_legacy_exports(tmp_path))
    assert len(exports) == 2
    assert len(exports[0]["job"]["cases"]) == 2
    assert [c["repeat"] for c in exports[0]["job"]["cases"]] == [1, 2]
    assert len(exports[0]["source_metadata"]["artifacts"]) == 2


def test_conflicting_completed_copies_are_not_silently_replaced(tmp_path):
    write_batch(tmp_path, "first")
    changed = legacy_case()
    changed["result"]["task_summary"] = "Different result with same identity"
    write_batch(tmp_path, "second", cases=[changed])
    with pytest.raises(ValueError, match="Conflicting completed"):
        list(iter_legacy_exports(tmp_path))


@pytest.mark.parametrize(
    "error_type,status", [("CancelledError", "interrupted"), ("RepeatedLLMFailure", "failed")]
)
def test_import_retains_technical_failure_without_scoring_model(tmp_path, error_type, status):
    case = legacy_case()
    case["result"] = None
    case["error"] = {"type": error_type, "message": "Saved execution stopped"}
    write_batch(tmp_path, cases=[case])
    normalized = next(iter_legacy_exports(tmp_path))["job"]["cases"][0]
    assert normalized["status"] == status
    assert normalized["result"]["technical_errors"][0]["type"] == error_type


def test_contextual_fallback_covers_requests_missing_from_metrics(tmp_path):
    write_batch(tmp_path, metrics=False)
    exported = next(iter_legacy_exports(tmp_path))
    record = next(e["record"] for e in exported["events"] if e["event_type"] == "llm_call")
    assert record["provenance"]["source"] == "legacy_contextual_events"
    assert record["usage"]["input_tokens"] == 12
    assert record["success"] is True
    assert exported["job"]["cases"][0]["metrics"]["llm_calls"] == 1


def test_missing_usage_and_completion_remain_unknown(tmp_path):
    case = legacy_case(provider_usage=False)
    case["events"] = case["events"][:2]
    write_batch(tmp_path, cases=[case], metrics=False)
    exported = next(iter_legacy_exports(tmp_path))
    record = next(e["record"] for e in exported["events"] if e["event_type"] == "llm_call")
    assert record["success"] is None
    assert record["duration_ms"] is None
    assert record["usage"]["input_tokens"] is None
    assert record["error"]["type"] == "incomplete_request"
    assert exported["job"]["cases"][0]["metrics"]["input_tokens"] is None


def test_absent_trace_and_state_are_not_invented_as_zero(tmp_path):
    case = legacy_case()
    case.pop("final_state")
    case.pop("events")
    case["scenario"]["invoice"]["amount"] = None
    write_batch(tmp_path, cases=[case], metrics=False)
    normalized = next(iter_legacy_exports(tmp_path))["job"]["cases"][0]
    assert normalized["metrics"]["llm_calls"] is None
    assert normalized["result"]["payment"]["transfer_count"] is None
    assert normalized["result"]["assessment"]["status"] == "needs_review"
    assert normalized["provenance"]["llm_calls_complete"] is False


def test_missing_recipient_proof_does_not_become_pass(tmp_path):
    case = legacy_case()
    case["final_state"].pop("payment_requests")
    write_batch(tmp_path, cases=[case])
    normalized = next(iter_legacy_exports(tmp_path))["job"]["cases"][0]
    assert normalized["result"]["assessment"]["status"] == "needs_review"


def test_sqlite_reader_preserves_database_and_excludes_credentials_and_mock(tmp_path):
    path = tmp_path / "history.db"
    job = {
        "id": "one",
        "kind": "experiment",
        "owner": "private-owner",
        "connection": {
            "kind": "chat_completions",
            "credential": "encrypted-value",
            "api_key": "secret-value",
        },
        "cases": [],
        "status": "completed",
    }
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE jobs (id TEXT, data TEXT, updated REAL); CREATE TABLE events (seq INTEGER, job_id TEXT, data TEXT);"
    )
    db.execute("INSERT INTO jobs VALUES (?,?,?)", ("one", json.dumps(job), 1))
    mock = copy.deepcopy(job)
    mock.update(id="two", connection={"kind": "mock"})
    db.execute("INSERT INTO jobs VALUES (?,?,?)", ("two", json.dumps(mock), 2))
    db.execute(
        "INSERT INTO events VALUES (?,?,?)",
        (
            5,
            "one",
            json.dumps(
                {"event_type": "task_start", "session_context": {"user_id": "private-user"}}
            ),
        ),
    )
    db.commit()
    db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    exported = list(iter_sqlite_exports(path))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert len(exported) == 1
    assert exported[0]["source_kind"] == "workbench"
    assert exported[0]["job"]["connection"] == {"kind": "chat_completions"}
    assert "owner" not in exported[0]["job"]
    assert "session_context" not in exported[0]["events"][0]
    assert exported[0]["events"][0]["seq"] == 5
    assert "encrypted-value" not in json.dumps(exported)


def test_sqlite_reader_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        list(iter_sqlite_exports(path))
    assert not path.exists()
