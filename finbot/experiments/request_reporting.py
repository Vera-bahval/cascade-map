"""Public request metrics without prompts, responses or credentials."""

from collections import defaultdict

from finbot.experiments.pricing import request_cost, sum_costs


def interaction_id(event):
    return ((event.get("record") or {}).get("metadata") or {}).get("interaction_id") or (
        event.get("event_data") or {}
    ).get("interaction_id")


def request_events(events):
    result, seen = [], set()
    for index, event in enumerate(events):
        if event.get("event_type") != "llm_call":
            continue
        identity = interaction_id(event)
        key = ("interaction", str(identity)) if identity else ("event", event.get("seq", index))
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def request_coverage_complete(case, events, calls):
    starts = [event for event in events if event.get("event_type") == "llm_request_start"]
    if any(not interaction_id(event) for event in starts):
        return False
    started = {str(interaction_id(event)) for event in starts}
    completed = {str(interaction_id(event)) for event in calls if interaction_id(event)}
    if started - completed:
        return False
    expected = (case.get("metrics") or {}).get("llm_calls")
    if expected is not None:
        return (
            isinstance(expected, int) and not isinstance(expected, bool) and expected == len(calls)
        )
    return bool(
        (case.get("provenance") or {}).get("llm_calls_complete") or started and started <= completed
    )


def request_summary(event, config):
    record = event.get("record") or {}
    usage = record.get("usage") or {}
    response = record.get("response")
    response = response if isinstance(response, dict) else {}
    metadata = record.get("metadata") or {}
    return {
        "event_seq": event.get("seq"),
        "at": event.get("at"),
        "agent": event.get("agent_name") or metadata.get("agent_name"),
        "model": response.get("model") or config.get("model"),
        "success": record.get("success"),
        "duration_ms": record.get("duration_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        **request_cost(usage, config),
    }


def billing_metrics(job, events):
    """Derive costs from a frozen config and usage-only events without changing snapshots."""
    config = job.get("config") or {}
    grouped = defaultdict(list)
    for event in events:
        if event.get("event_type") in {"llm_call", "llm_request_start"}:
            grouped[event.get("case_id")].append(event)
    cases = {}
    for case in job.get("cases", []):
        case_events = grouped[case.get("id")]
        calls = request_events(case_events)
        cases[case.get("id")] = sum_costs(
            [
                request_cost((event.get("record") or {}).get("usage") or {}, config)
                for event in calls
            ],
            config,
            complete=case.get("status") not in {"queued", "running"}
            and request_coverage_complete(case, case_events, calls),
        )
    return {
        "cases": cases,
        "run": sum_costs(cases.values(), config, complete=bool(cases)),
    }
