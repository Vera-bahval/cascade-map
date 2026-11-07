"""Deterministic, credential-free projection of saved experiment evidence."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from finbot.experiments.catalog import EXPECTATIONS
from finbot.experiments.pricing import request_cost, sum_costs
from finbot.experiments.reporting import case_report
from finbot.experiments.request_reporting import request_coverage_complete, request_events

SECRET_KEYS = {
    "apikey",
    "credential",
    "credentials",
    "authorization",
    "password",
    "passwd",
    "secret",
    "secretkey",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "token",
    "auth",
    "headers",
    "cookie",
    "cookies",
    "setcookie",
}
IDENTITY_KEYS = {
    "owner",
    "session",
    "sessioncontext",
    "sessionid",
    "sessiontoken",
    "userid",
    "username",
    "useremail",
    "namespace",
}


def _key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _blocked(value):
    name = _key(value)
    return name in SECRET_KEYS | IDENTITY_KEYS or name.endswith(("apikey", "password", "secretkey"))


def _embedded(value):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return None


def _secrets(value):
    found = set()

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if _blocked(key) and _key(key) != "namespace" and isinstance(child, str):
                    minimum = 8 if _key(key) in IDENTITY_KEYS else 4
                    if len(child) >= minimum:
                        found.add(child)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        else:
            parsed = _embedded(item)
            if parsed is not None:
                visit(parsed)

    visit(value)
    return sorted(found, key=len, reverse=True)


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): sanitize(v, secrets) for k, v in value.items() if not _blocked(k)}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        parsed = _embedded(value)
        if parsed is not None:
            value = json.dumps(sanitize(parsed, secrets), ensure_ascii=False, allow_nan=False)
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", value)
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _id(*parts):
    return str(uuid5(NAMESPACE_URL, "cascademap:" + json.dumps(parts, ensure_ascii=False)))


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _tokens(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number < 2**63 else None


def _confidence(value):
    number = _number(value)
    return number if number is not None and number <= 1 else None


def _time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.isoformat() if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _elapsed(start, finish):
    if not start or not finish:
        return None
    seconds = (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
    return round(seconds * 1000, 3) if seconds >= 0 else None


def _millis(value):
    number = _number(value)
    return round(number * 1000, 3) if number is not None else None


def _span_id(event):
    return event.get("agent_run_id") or (event.get("record", {}).get("metadata") or {}).get(
        "agent_run_id"
    )


def _agent_name(event):
    return event.get("agent_name") or (event.get("record", {}).get("metadata") or {}).get(
        "agent_name"
    )


def _interaction(event):
    return (event.get("record", {}).get("metadata") or {}).get("interaction_id") or (
        event.get("event_data") or {}
    ).get("interaction_id")


def _agent_row(case_id, source_id, agent_name, order, provenance):
    return {
        "id": _id(case_id, "agent", source_id),
        "case_id": case_id,
        "parent_agent_run_id": None,
        "source_agent_run_id": source_id,
        "agent_name": agent_name or "unknown",
        "step_order": order,
        "execution_status": "unknown",
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "summary": None,
        "result": {},
        "input": {},
        "heuristic_confidence": None,
        "model_confidence": None,
        "metrics": {},
        "provenance": provenance,
    }


def _reported_status(result):
    status = (result or {}).get("task_status")
    return "failed" if status in {"failed", "error"} else "completed"


def _agents(case_id, case, events):
    rows, explicit, attribution, interactions = [], {}, {}, {}
    for index, event in enumerate(events):
        sid = _span_id(event)
        if not sid:
            continue
        sid = str(sid)
        if sid not in explicit:
            row = _agent_row(
                case_id, sid, _agent_name(event), len(rows), {"identity": "explicit_span"}
            )
            explicit[sid] = row
            rows.append(row)
        row = explicit[sid]
        if _agent_name(event):
            row["agent_name"] = _agent_name(event)
        meta = event.get("record", {}).get("metadata") or {}
        parent = event.get("parent_agent_run_id") or meta.get("parent_agent_run_id")
        if parent and str(parent) != sid:
            row["parent_agent_run_id"] = _id(case_id, "agent", str(parent))
        data = event.get("event_data") or {}
        kind = event.get("event_type")
        if kind == "agent_span_start":
            row.update(started_at=_time(event.get("at")), input=data.get("input") or {})
        elif kind == "agent_span_end":
            row.update(
                finished_at=_time(event.get("at")),
                duration_ms=_number(data.get("duration_ms")),
                execution_status=data.get("status") or "unknown",
                result=data.get("result") or {},
            )
            if data.get("error"):
                row["provenance"]["error"] = data["error"]
        attribution[index] = row["id"]
        if _interaction(event):
            interactions[str(_interaction(event))] = row["id"]

    active, ambiguous = defaultdict(list), set()
    for index, event in enumerate(events):
        if _span_id(event):
            continue
        name, kind = _agent_name(event), event.get("event_type")
        data = event.get("event_data") or {}
        if not name:
            continue
        if kind == "task_start":
            source_id = f"lifecycle:{event.get('seq', index + 1)}:{name}"
            row = _agent_row(
                case_id, source_id, name, len(rows), {"identity": "lifecycle_boundaries"}
            )
            row.update(started_at=_time(event.get("at")), input=data.get("task_data") or {})
            roots = active.get("orchestrator_agent", [])
            if name != "orchestrator_agent" and len(roots) == 1:
                row["parent_agent_run_id"] = roots[0]["id"]
            rows.append(row)
            active[name].append(row)
            if len(active[name]) > 1:
                ambiguous.add(name)
                for candidate in active[name]:
                    candidate["provenance"]["ambiguous_boundaries"] = True
        candidates = active.get(name, [])
        row = candidates[0] if len(candidates) == 1 and name not in ambiguous else None
        if row:
            attribution[index] = row["id"]
            if _interaction(event):
                interactions[str(_interaction(event))] = row["id"]
        if kind == "task_completion" and candidates:
            completed = active[name].pop(0)
            if row:
                completed.update(
                    finished_at=_time(event.get("at")),
                    result=data.get("task_result") or {},
                    execution_status=_reported_status(data.get("task_result")),
                )
                completed["duration_ms"] = _elapsed(
                    completed["started_at"], completed["finished_at"]
                )
            if not active[name]:
                ambiguous.discard(name)

    by_name = defaultdict(list)
    for row in rows:
        by_name[row["agent_name"]].append(row)
    used = defaultdict(int)
    chain = (case.get("result") or {}).get("agent_chain") or []
    for index, step in enumerate(chain):
        name = step.get("agent") or "unknown"
        occurrence = used[name]
        used[name] += 1
        candidates = by_name[name]
        if occurrence < len(candidates):
            row = candidates[occurrence]
        else:
            row = _agent_row(
                case_id,
                f"chain:{index + 1}:{name}",
                name,
                index + 1,
                {"identity": "agent_chain_only"},
            )
            row["execution_status"] = (
                "completed"
                if step.get("success") is True
                else "failed" if step.get("success") is False else "unknown"
            )
            rows.append(row)
        row["step_order"] = step.get("order", index + 1)
        row["heuristic_confidence"] = _confidence(step.get("confidence"))
        if row["heuristic_confidence"] is not None:
            row["provenance"]["confidence_source"] = "heuristic_task_summary"
        row["summary"] = step.get("reasoning") or (row["result"] or {}).get("task_summary")
        if not row["result"]:
            row["result"] = {
                "task_summary": step.get("reasoning"),
                "reported_success": step.get("success"),
                "detector_errors": step.get("errors") or [],
            }
    known_ids = {row["id"] for row in rows}
    for row in rows:
        if row["parent_agent_run_id"] not in known_ids:
            row["parent_agent_run_id"] = None
        row["summary"] = row["summary"] or (row["result"] or {}).get("task_summary")
        if row["execution_status"] == "unknown" and row["started_at"]:
            row["provenance"]["missing_end"] = True
    return rows, attribution, interactions


def _attribute(event, index, agents, attribution, interactions):
    if _span_id(event):
        sid = str(_span_id(event))
        return next((a["id"] for a in agents if a["source_agent_run_id"] == sid), None)
    interaction = _interaction(event)
    if interaction and str(interaction) in interactions:
        return interactions[str(interaction)]
    if index in attribution:
        return attribution[index]
    at, name = _time(event.get("at")), _agent_name(event)
    if not at or not name:
        return None
    point = datetime.fromisoformat(at)
    matches = [
        a
        for a in agents
        if a["agent_name"] == name
        and a["started_at"]
        and a["finished_at"]
        and not a["provenance"].get("ambiguous_boundaries")
        and datetime.fromisoformat(a["started_at"])
        <= point
        <= datetime.fromisoformat(a["finished_at"])
    ]
    return matches[0]["id"] if len(matches) == 1 else None


def _calls(case_id, config, events, agents, attribution, interactions):
    result, seen = [], set()
    for index, event in enumerate(events):
        if event.get("event_type") != "llm_call":
            continue
        record = event.get("record") or {}
        metadata = record.get("metadata") or {}
        interaction = _interaction(event)
        source_id = str(interaction) if interaction else f"event:{event.get('seq', index + 1)}"
        if source_id in seen:
            continue
        seen.add(source_id)
        usage, response = record.get("usage") or {}, record.get("response") or {}
        provenance = {**(record.get("provenance") or {}), "canonical_source": "llm_call"}
        billing = request_cost(usage, config)
        provenance["pricing"] = billing["pricing"]
        row = {
            "id": _id(case_id, "call", source_id),
            "case_id": case_id,
            "agent_run_id": _attribute(event, index, agents, attribution, interactions),
            "source_interaction_id": str(interaction) if interaction else None,
            "call_index": len(result) + 1,
            "agent_name": _agent_name(event),
            "occurred_at": _time(event.get("at")),
            "success": record.get("success") if isinstance(record.get("success"), bool) else None,
            "duration_ms": _number(record.get("duration_ms")),
            "model_requested": metadata.get("model_requested")
            or (record.get("effective_params") or {}).get("model")
            or config.get("model"),
            "model_returned": metadata.get("model_returned")
            or (response.get("model") if isinstance(response, dict) else None),
            "input_tokens": _tokens(usage.get("input_tokens")),
            "output_tokens": _tokens(usage.get("output_tokens")),
            "cached_tokens": _tokens(usage.get("cached_input_tokens")),
            "reasoning_tokens": _tokens(usage.get("reasoning_tokens")),
            "estimated_input_tokens": _tokens(record.get("estimated_input_tokens")),
            "estimated_output_tokens": _tokens(record.get("estimated_output_tokens")),
            "cost": billing["cost"],
            "currency": billing["currency"],
            "cost_basis": billing["cost_basis"],
            "effective_params": record.get("effective_params") or {},
            "request": record.get("request") or {},
            "response": response,
            "error": record.get("error") or {},
            "provenance": provenance,
        }
        provenance.setdefault(
            "usage_source",
            (
                "provider"
                if row["input_tokens"] is not None or row["output_tokens"] is not None
                else (
                    "estimated"
                    if row["estimated_input_tokens"] is not None
                    or row["estimated_output_tokens"] is not None
                    else "unavailable"
                )
            ),
        )
        if row["agent_run_id"] is None:
            provenance["agent_attribution"] = "unavailable"
        result.append(row)
    return result


def _complete_calls(case, events, calls):
    starts = {
        str(_interaction(e))
        for e in events
        if e.get("event_type") == "llm_request_start" and _interaction(e)
    }
    completed = {c["source_interaction_id"] for c in calls if c["source_interaction_id"]}
    if starts - completed:
        return False
    expected = (case.get("metrics") or {}).get("llm_calls")
    if expected is not None:
        return _tokens(expected) == len(calls)
    return bool(
        (case.get("provenance") or {}).get("llm_calls_complete") or starts and starts <= completed
    )


def _metrics(calls, complete, known_identity):
    known = complete and known_identity
    metrics = {
        "llm_calls": len(calls) if calls or known else None,
        "known_llm_calls": len(calls),
        "call_coverage": "complete" if known else "observed_only" if calls else "unavailable",
        "llm_failures": sum(c["success"] is False for c in calls) if calls or known else None,
        "unknown_call_statuses": sum(c["success"] is None for c in calls),
        "usage_missing_calls": sum(
            c["input_tokens"] is None or c["output_tokens"] is None for c in calls
        ),
    }
    for field in (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "duration_ms",
    ):
        values = [c[field] for c in calls if c[field] is not None]
        name = "llm_duration_ms" if field == "duration_ms" else field
        metrics[name + "_known"] = round(sum(values), 3)
        metrics[name] = round(sum(values), 3) if known and len(values) == len(calls) else None
    return metrics


def _location(case, report):
    detector = report.get("detector") or {}
    first = detector.get("trigger") or {}
    first_agent = first.get("agent")
    if not first_agent and first.get("order") is not None:
        matches = [
            step.get("agent")
            for step in report.get("timeline") or []
            if step.get("order") == first["order"]
        ]
        if len(matches) == 1:
            first_agent = matches[0]
    origin = report.get("error_location") or {}
    identified = origin.get("status") == "identified"
    review = case.get("assessment") or {}
    valid_review = report["cascade"]["status"] == "manual"
    return {
        "first_signal_agent": first_agent,
        "first_signal_step": first.get("order"),
        "basis": (
            origin.get("basis")
            if identified
            else "heuristic_detector_signal" if first else "unavailable"
        ),
        "detected_origin_agent": origin.get("agent") if identified else None,
        "detected_origin_step": origin.get("order") if identified else None,
        "detected_origin_event_seq": origin.get("event_seq") if identified else None,
        "detected_origin_basis": origin.get("basis") if identified else None,
        "detected_origin_evidence": origin.get("evidence") or [] if identified else [],
        "propagation": origin.get("propagation") or [],
        "detector_version": detector.get("version"),
        "confirmed_origin_agent": review.get("origin_agent") if valid_review else None,
        "confirmed_origin_step": review.get("origin_step") if valid_review else None,
        "confirmed_stopped_agent": review.get("stopped_agent") if valid_review else None,
        "explanation": detector.get("explanation"),
        "limitation": detector.get("limitation"),
    }


def _source_metadata(value):
    result = {}
    for key, item in (value or {}).items():
        if isinstance(item, str) and ("path" in key.lower() or "file" in key.lower()):
            result[key] = Path(item).name
        else:
            result[key] = item
    return result


def build_archive(
    job,
    events,
    *,
    source_kind="workbench",
    source_run_id=None,
    submitted_by=None,
    source_metadata=None,
):
    secrets = _secrets([job, events, source_metadata])
    job, events = sanitize(job, secrets), sanitize(list(events), secrets)
    source_run_id = str(source_run_id or job.get("id") or "")
    if not source_run_id:
        raise ValueError("A stable source run ID is required")
    run_id = _id(source_kind, source_run_id)
    config = {
        k: v
        for k, v in (job.get("config") or {}).items()
        if k not in {"connection_id", "id", "created_at"}
    }
    demo = (job.get("connection") or {}).get("kind") == "mock" or bool(
        (job.get("metrics") or {}).get("demonstration")
    )
    result = {
        "series": job.get("series_snapshot"),
        "run": {
            "id": run_id,
            "source_kind": source_kind,
            "source_run_id": source_run_id,
            "submitted_by": submitted_by,
            "model": config.get("model"),
            "protocol": (job.get("connection") or {}).get("kind"),
            "status": job.get("status") or "unknown",
            "started_at": _time(job.get("started_at")),
            "finished_at": _time(job.get("finished_at")),
            "is_demo": demo,
            "config": config,
            "metrics": job.get("metrics") or {},
            "metadata": {
                "projection_version": 1,
                "source_updated_at": job.get("updated_at"),
                "created_at": job.get("created_at"),
                "code_revision": job.get("code_revision"),
                "scenario_hash": job.get("scenario_hash"),
                "execution": job.get("execution") or {},
                "series_id": job.get("series_id"),
                "evaluation_policy": job.get("evaluation_policy"),
                "source": _source_metadata(sanitize(source_metadata or {}, secrets)),
            },
        },
        "cases": [],
        "agents": [],
        "calls": [],
        "events": [],
        "iterations": [],
        "evaluations": [],
        "case_observations": [],
        "series_runs": (
            [{"run_id": run_id, "series_id": job["series_id"]}] if job.get("series_id") else []
        ),
    }
    grouped = defaultdict(list)
    for event in events:
        grouped[str(event.get("case_id"))].append(event)
    for index, case in enumerate(job.get("cases") or []):
        source_case_id = str(case.get("id") or case.get("workflow_id") or "")
        if not source_case_id:
            raise ValueError("A stable source case ID is required")
        case_id = _id(run_id, "case", source_case_id)
        case_events = grouped[source_case_id]
        report = case_report(case, case_events, demo)
        agents, attribution, interactions = _agents(case_id, case, case_events)
        calls = _calls(case_id, config, case_events, agents, attribution, interactions)
        complete = _complete_calls(case, case_events, calls)
        unassigned = any(c["agent_run_id"] is None for c in calls)
        for agent in agents:
            own = [c for c in calls if c["agent_run_id"] == agent["id"]]
            agent["metrics"] = _metrics(
                own,
                complete,
                not unassigned
                and agent["provenance"]["identity"] != "agent_chain_only"
                and not agent["provenance"].get("ambiguous_boundaries"),
            )
        details = case.get("result") or {}
        from finbot.experiments.observations import case_observations

        observations = case.get("observations") or case_observations(details, case_events)
        result["cases"].append(
            {
                "id": case_id,
                "run_id": run_id,
                "source_case_id": source_case_id,
                "scenario_id": case.get("scenario_id") or "unknown",
                "repeat_index": case.get("repeat", 1),
                "execution_status": case.get("status") or "unknown",
                "started_at": _time(case.get("started_at")),
                "finished_at": _time(case.get("finished_at")),
                "duration_ms": _millis(case.get("duration_seconds")),
                "expected_behavior": (details.get("assessment") or {}).get("expected_behavior")
                or EXPECTATIONS.get(case.get("scenario_id")),
                "report": report,
                "result": details,
                "metrics": {
                    **(case.get("metrics") or {}),
                    **sum_costs(
                        calls,
                        config,
                        complete=complete
                        and case.get("status") not in {"queued", "running"}
                        and request_coverage_complete(
                            case, case_events, request_events(case_events)
                        ),
                    ),
                },
                "human_review": case.get("assessment") or {},
                "scenario_snapshot": case.get("scenario_snapshot") or {},
                "cascade_location": _location(case, report),
                "provenance": {
                    **(case.get("provenance") or {}),
                    "call_coverage_complete": complete,
                    "unassigned_llm_calls": sum(c["agent_run_id"] is None for c in calls),
                    "report_version": report.get("version"),
                    "detector_version": (report.get("detector") or {}).get("version"),
                },
            }
        )
        seen = set()
        for event_index, event in enumerate(case_events):
            source_seq = str(event.get("seq", event_index + 1))
            if source_seq in seen:
                continue
            seen.add(source_seq)
            result["events"].append(
                {
                    "id": _id(case_id, "event", source_seq),
                    "case_id": case_id,
                    "agent_run_id": _attribute(
                        event, event_index, agents, attribution, interactions
                    ),
                    "source_seq": source_seq,
                    "occurred_at": _time(event.get("at")),
                    "event_type": event.get("event_type") or "unknown",
                    "payload": event,
                }
            )
        result["agents"].extend(agents)
        result["calls"].extend(calls)
        result["case_observations"].append({"case_id": case_id, "observations": observations})
        span_ids = {
            a["source_agent_run_id"]: a["id"] for a in agents if a.get("source_agent_run_id")
        }
        for iteration in observations.get("iterations", []):
            iteration_id = _id(
                case_id, "iteration", iteration["source_agent_run_id"], iteration["iteration_index"]
            )
            result["iterations"].append(
                {
                    "id": iteration_id,
                    "case_id": case_id,
                    "agent_run_id": span_ids.get(iteration["source_agent_run_id"]),
                    "source_agent_run_id": iteration["source_agent_run_id"],
                    "agent_name": iteration["agent_name"],
                    "iteration_index": iteration["iteration_index"],
                    "started_at": _time(iteration.get("started_at")),
                    "finished_at": _time(iteration.get("finished_at")),
                    "execution_status": iteration["status"],
                    "vanilla_confidence": iteration.get("vanilla_confidence"),
                    "lexical_confidence": iteration.get("lexical_confidence"),
                    "trace": {k: v for k, v in iteration.items() if k != "judge"},
                }
            )
            judge = iteration.get("judge") or {"status": "not_requested"}
            result["evaluations"].append(
                {
                    "iteration_id": iteration_id,
                    "status": judge["status"],
                    "confidence": judge.get("confidence"),
                    "error_present": judge.get("error_present"),
                    "assessment": judge,
                }
            )
    result["run"]["metrics"] = {
        **result["run"]["metrics"],
        **sum_costs(
            [case["metrics"] for case in result["cases"]],
            config,
            complete=bool(result["cases"]),
        ),
    }
    return sanitize(result, secrets)
