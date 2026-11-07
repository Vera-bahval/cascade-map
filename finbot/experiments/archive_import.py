"""Read-only adapters for saved workbench and earlier batch results."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from finbot.experiments.catalog import EXPECTATIONS, RUBRIC_VERSION
from finbot.experiments.executor import assess_outcome

IGNORED_DIRECTORIES = {"prepared", "diagnostics", "smoke", "tests", ".git", ".venv"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
CONFIG_FIELDS = (
    "model",
    "provider",
    "temperature",
    "configured_temperature",
    "temperature_policy",
    "reasoning_effort",
    "max_output_tokens",
    "quantization",
    "runtime",
    "model_revision",
    "context_length",
    "context_length_basis",
    "api_style",
    "inference_parameters",
    "inference_parameters_basis",
    "configured_agent_max_iterations",
    "orchestrator_max_iterations",
    "observed_max_iterations_by_agent",
    "repetitions_per_scenario",
    "selected_scenario_ids",
    "llm_timeout_seconds",
    "scenario_timeout_seconds",
    "max_consecutive_llm_failures",
)


def _json_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _number(value):
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def _first_number(*values):
    return next((n for value in values if (n := _number(value)) is not None), None)


def _clean_event(event):
    event = copy.deepcopy(event)
    event.pop("session_context", None)
    event.pop("args", None)
    return event


def _usage(call, response):
    raw = call.get("provider_usage") or (response.get("metadata") or {}).get("usage") or {}
    raw = raw if isinstance(raw, dict) else {}
    incoming = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    outgoing = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    return {
        "input_tokens": _first_number(
            call.get("provider_input_tokens"), raw.get("input_tokens"), raw.get("prompt_tokens")
        ),
        "output_tokens": _first_number(
            call.get("provider_output_tokens"),
            raw.get("output_tokens"),
            raw.get("completion_tokens"),
        ),
        "cached_input_tokens": _first_number(
            call.get("provider_cached_input_tokens"), incoming.get("cached_tokens")
        ),
        "reasoning_tokens": _first_number(
            call.get("provider_reasoning_tokens"), outgoing.get("reasoning_tokens")
        ),
    }


def _call_record(call, start, end, workflow_id, source):
    start_data = (start or {}).get("event_data") or {}
    end_data = (end or {}).get("event_data") or {}
    request = _json_object(start_data.get("request_dump") or end_data.get("request_dump"))
    response = _json_object(end_data.get("response_dump"))
    metadata = response.get("metadata") or {}
    success = call.get("response_success", end_data.get("success", response.get("success")))
    if success is None and end:
        kind = end.get("event_type", "")
        success = (
            True
            if kind == "llm_request_success"
            else False if kind in {"llm_request_failure", "llm_request_error"} else None
        )
    elapsed = _number(call.get("duration_seconds"))
    duration_ms = elapsed * 1000 if elapsed is not None else _number(end_data.get("duration_ms"))
    effective = call.get("effective_parameters")
    if not isinstance(effective, dict):
        effective = metadata.get("effective_parameters")
    params_source = "provider_response" if isinstance(effective, dict) else "unavailable"
    usage = _usage(call, response)
    estimated_in = _number(call.get("estimated_input_tokens"))
    estimated_out = _number(call.get("estimated_output_tokens"))
    usage_source = (
        "provider"
        if any(v is not None for v in usage.values())
        else "estimated" if estimated_in is not None or estimated_out is not None else "unavailable"
    )
    model = call.get("model") or start_data.get("model") or end_data.get("model")
    returned_model = call.get("actual_model") or metadata.get("model")
    error = call.get("response_error") or end_data.get("error") or response.get("error")
    if not end and success is None:
        error = {"type": "incomplete_request", "message": "No saved completion for this request"}
    return {
        "success": success,
        "duration_ms": duration_ms,
        "usage": usage,
        "estimated_input_tokens": estimated_in,
        "estimated_output_tokens": estimated_out,
        "effective_params": copy.deepcopy(effective) if isinstance(effective, dict) else {},
        "request": request or None,
        "response": response or None,
        "error": error,
        "metadata": {
            "agent_name": call.get("agent")
            or start_data.get("agent_name")
            or (start or end or {}).get("agent_name"),
            "interaction_id": call.get("interaction_id")
            or start_data.get("interaction_id")
            or end_data.get("interaction_id"),
            "workflow_id": workflow_id,
            "model_requested": model,
            "model_returned": returned_model,
        },
        "provenance": {
            "source": source,
            "usage_source": usage_source,
            "effective_params_source": params_source,
            "configured_temperature": call.get("temperature"),
        },
    }


def _normalize_events(raw_events, metric, case_id):
    events = [_clean_event(e) for e in raw_events if isinstance(e, dict)]
    starts, ends = {}, {}
    for index, event in enumerate(events):
        data = event.get("event_data") or {}
        interaction = data.get("interaction_id")
        if not interaction:
            continue
        if event.get("event_type") == "llm_request_start":
            starts.setdefault(interaction, (index, event))
        elif event.get("event_type") in {
            "llm_request_success",
            "llm_request_failure",
            "llm_request_error",
        }:
            ends[interaction] = (index, event)
    raw_calls = metric.get("calls")
    raw_calls = raw_calls if isinstance(raw_calls, list) else []
    records, seen = [], set()
    for call_index, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        interaction = call.get("interaction_id")
        identity = interaction or f"metric-call-{call_index}"
        if identity in seen:
            continue
        seen.add(identity)
        start = starts.get(interaction, (None, None))[1]
        end_index, end = ends.get(interaction, (None, None))
        record = _call_record(call, start, end, case_id, "legacy_metrics")
        records.append((end_index, end, identity, record))
    for interaction in dict.fromkeys([*starts, *ends]):
        if interaction in seen:
            continue
        start = starts.get(interaction, (None, None))[1]
        end_index, end = ends.get(interaction, (None, None))
        record = _call_record(
            {"interaction_id": interaction}, start, end, case_id, "legacy_contextual_events"
        )
        records.append((end_index, end, interaction, record))
    by_end = defaultdict(list)
    tail = []
    for end_index, end, identity, record in records:
        canonical = {
            "case_id": case_id,
            "at": end.get("at") if end else None,
            "event_type": "llm_call",
            "source_id": f"llm:{identity}",
            "record": record,
        }
        if end_index is None:
            tail.append(canonical)
        else:
            by_end[end_index].append(canonical)
    output = []
    for index, event in enumerate(events):
        event.update(case_id=case_id, seq=index * 2, source_id=f"event:{index}")
        output.append(event)
        for canonical in by_end[index]:
            canonical["seq"] = index * 2 + 1
            output.append(canonical)
    for index, canonical in enumerate(tail):
        canonical["seq"] = len(events) * 2 + index
        output.append(canonical)
    return output, [record for _, _, _, record in records]


def _metrics(records, legacy, duration):
    expected = _number(legacy.get("llm_calls_started"))
    count = int(expected) if expected is not None else len(records) if records else None
    missing_records = max(0, (count or 0) - len(records))
    result = {
        "llm_calls": count,
        "llm_failures": sum(r.get("success") is False for r in records),
        "llm_unfinished": sum(r.get("success") is None for r in records),
        "duration_seconds": duration,
        "usage_missing_calls": missing_records
        + sum(
            r["usage"].get("input_tokens") is None or r["usage"].get("output_tokens") is None
            for r in records
        ),
    }
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
        values = [r["usage"].get(key) for r in records]
        known = sum(v for v in values if v is not None)
        result[key + "_known"] = known
        result[key] = (
            known
            if not missing_records and all(v is not None for v in values) and records
            else None
        )
    for key in ("estimated_input_tokens", "estimated_output_tokens"):
        values = [r.get(key) for r in records]
        result[key] = (
            sum(values)
            if records and not missing_records and all(v is not None for v in values)
            else None
        )
    return result


def _case_status(raw):
    error = raw.get("error")
    if error:
        return "interrupted" if _json_object(error).get("type") == "CancelledError" else "failed"
    if raw.get("status") in TERMINAL:
        return raw["status"]
    task_status = _json_object(raw.get("result")).get("task_status")
    return (
        "completed"
        if task_status == "success"
        else "failed" if task_status in {"failed", "error", "failure"} else "unknown"
    )


def _normalize_case(raw, metric, case_id, repeat):
    scenario = _json_object(raw.get("scenario"))
    scenario_id = scenario.get("id") or raw.get("scenario_id") or "unknown"
    status = _case_status(raw)
    state = raw.get("final_state")
    analysis = copy.deepcopy(raw.get("cascade_analysis") or {})
    amount = _json_object(scenario.get("invoice")).get("amount")
    amount_valid = (
        isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount)
    )
    if (
        isinstance(state, dict)
        and isinstance(state.get("invoices"), list)
        and isinstance(state.get("transactions"), list)
        and scenario.get("id")
        and amount_valid
    ):
        payment, assessment = assess_outcome(scenario, state, analysis)
    else:
        payment = {"transfer_count": None, "total_amount": None, "invoice_status": None}
        assessment = {
            "status": "needs_review",
            "expected_behavior": EXPECTATIONS.get(scenario_id, "Нужен разбор сценария"),
            "reasons": ["Полное конечное состояние не сохранено."],
            "detector_type": analysis.get("cascade_type"),
        }
    error = raw.get("error")
    errors = [copy.deepcopy(error)] if error else []
    if status != "completed" and not errors:
        errors = [
            {
                "type": "incomplete_legacy_case",
                "message": "Saved task did not report successful completion",
            }
        ]
    duration = _number(raw.get("duration_seconds"))
    events, records = _normalize_events(raw.get("events") or [], metric, case_id)
    metrics = _metrics(records, metric, duration)
    return {
        "id": case_id,
        "scenario_id": scenario_id,
        "repeat": repeat,
        "status": status,
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "duration_seconds": duration,
        "scenario_snapshot": copy.deepcopy(scenario),
        "result": {
            "agent_result": copy.deepcopy(raw.get("result")),
            "agent_chain": copy.deepcopy(raw.get("agent_chain") or []),
            "cascade_analysis": analysis,
            "initial_state": copy.deepcopy(raw.get("initial_state")),
            "final_state": copy.deepcopy(state),
            "payment": payment,
            "assessment": assessment,
            "technical_errors": errors,
            "sandbox": "legacy_saved_run",
        },
        "metrics": metrics,
        "provenance": {
            "source": "legacy_batch",
            "assessment_basis": "recomputed_from_saved_state",
            "assessment_rubric": RUBRIC_VERSION,
            "legacy_execution_status": metric.get("execution_status"),
            "author_expected_cascade_type": scenario.get("expected_cascade_type"),
            "llm_calls_complete": metrics["llm_calls"] is not None
            and len(records) == metrics["llm_calls"],
        },
    }, events


def _paths(root):
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.rglob("results.json"))
    for path in paths:
        relative = path.relative_to(root) if root.is_dir() else Path(path.name)
        if not any(
            part.lower() in IGNORED_DIRECTORIES or part.lower().startswith("smoke")
            for part in relative.parts
        ):
            yield path


def _latest_evidence_time(values):
    times = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is not None:
            times.append(parsed.astimezone(timezone.utc))
    return max(times).isoformat().replace("+00:00", "Z") if times else None


def iter_legacy_exports(root):
    """Yield historical batches once; refuse conflicting copies of a completed case."""
    batches = {}
    for path in _paths(root):
        contents = path.read_bytes()
        data = json.loads(contents)
        if (
            not isinstance(data, dict)
            or not data.get("run_id")
            or not isinstance(data.get("results"), list)
        ):
            continue
        if data.get("provider") == "mock" or data.get("model") == "mock":
            continue
        metric_path = path.with_name("metrics.json")
        metric_contents = metric_path.read_bytes() if metric_path.exists() else None
        metric_data = json.loads(metric_contents) if metric_contents else {}
        metric_groups = defaultdict(deque)
        for metric in metric_data.get("results", []):
            metric_groups[metric.get("scenario_id")].append(metric)
        source = {
            "file": f"{path.parent.name}/{path.name}",
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
        if metric_contents:
            source["metrics_sha256"] = hashlib.sha256(metric_contents).hexdigest()
        key = str(data["run_id"])
        if key not in batches:
            config = {name: copy.deepcopy(data[name]) for name in CONFIG_FIELDS if name in data}
            batches[key] = {
                "data": data,
                "config": config,
                "cases": {},
                "sources": [],
                "timestamps": [],
            }
        batch = batches[key]
        if batch["data"].get("model") != data.get("model") or batch["data"].get(
            "provider"
        ) != data.get("provider"):
            raise ValueError(f"Conflicting metadata for legacy run {key}")
        batch["sources"].append(source)
        batch["timestamps"].append(data.get("finished_at"))
        if len(data["results"]) > len(batch["data"]["results"]):
            batch["data"] = data
        repeats = defaultdict(int)
        for index, raw in enumerate(data["results"]):
            scenario_id = (
                _json_object(raw.get("scenario")).get("id") or raw.get("scenario_id") or "unknown"
            )
            repeats[scenario_id] += 1
            repeat = raw.get("repeat") or raw.get("repetition") or repeats[scenario_id]
            case_id = str(raw.get("workflow_id") or f"{key}:{scenario_id}:{repeat}:{index}")
            metric = metric_groups[scenario_id].popleft() if metric_groups[scenario_id] else {}
            normalized, events = _normalize_case(raw, metric, case_id, repeat)
            previous = batch["cases"].get(case_id)
            if previous:
                old = previous[2]
                if old == raw:
                    if len(events) > len(previous[1]):
                        batch["cases"][case_id] = (normalized, events, raw)
                    continue
                if old.get("finished_at") and raw.get("finished_at"):
                    raise ValueError(f"Conflicting completed copies of legacy case {case_id}")
                if old.get("finished_at") or len(old.get("events") or []) > len(
                    raw.get("events") or []
                ):
                    continue
            batch["cases"][case_id] = (normalized, events, raw)
    for run_id, batch in batches.items():
        data = batch["data"]
        cases = [item[0] for item in batch["cases"].values()]
        events = [event for item in batch["cases"].values() for event in item[1]]
        updated_at = _latest_evidence_time(
            batch["timestamps"]
            + [case.get("finished_at") for case in cases]
            + [event.get("at") for event in events]
        )
        status = data.get("status") or (
            "completed" if cases and all(c["status"] == "completed" for c in cases) else "unknown"
        )
        yield {
            "source_kind": "legacy",
            "source_run_id": run_id,
            "source_metadata": {
                "artifacts": batch["sources"],
                "original_provider": data.get("provider"),
                "runner_sha256": data.get("runner_sha256"),
                "agent_sources_sha256": data.get("agent_sources_sha256"),
                "legacy_status": data.get("status"),
                "updated_at_source": "latest_evidence_timestamp" if updated_at else "unavailable",
            },
            "job": {
                "id": run_id,
                "kind": "experiment",
                "status": status,
                "created_at": data.get("started_at"),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "updated_at": updated_at,
                "config": batch["config"],
                "connection": {"kind": data.get("api_style")},
                "code_revision": data.get("runner_sha256") or "unknown",
                "scenario_hash": data.get("scenario_file_sha256") or "",
                "cases": cases,
                "metrics": {
                    "total_cases": len(cases),
                    "completed_cases": sum(c["status"] == "completed" for c in cases),
                    "failed_cases": sum(c["status"] == "failed" for c in cases),
                },
            },
            "events": events,
        }


def iter_sqlite_exports(path):
    """Read only existing experiment records; never open or decrypt saved credentials."""
    path = Path(path).expanduser().resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for row in connection.execute("SELECT id,data FROM jobs ORDER BY updated,id"):
            job = json.loads(row[1])
            if (
                job.get("kind") != "experiment"
                or (job.get("connection") or {}).get("kind") == "mock"
            ):
                continue
            job.pop("owner", None)
            job["connection"] = {"kind": (job.get("connection") or {}).get("kind")}
            if job.get("series_id"):
                from finbot.experiments.store import Store

                job["series_snapshot"] = Store._series(connection, job["series_id"])
            events = []
            for seq, raw in connection.execute(
                "SELECT seq,data FROM events WHERE job_id=? ORDER BY seq", (row[0],)
            ):
                event = _clean_event(json.loads(raw))
                event["seq"] = seq
                events.append(event)
            yield {
                "job": job,
                "events": events,
                "source_kind": "workbench",
                "source_run_id": str(row[0]),
                "source_metadata": {"file": path.name, "format": "workbench_sqlite"},
            }
    finally:
        connection.close()
