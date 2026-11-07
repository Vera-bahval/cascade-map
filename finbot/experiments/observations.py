"""Passive observations; never supplied to the agent or used to control its loop."""

import re

from finbot.agents.cascade_signals import RISK_KEYWORDS, _infer_confidence

LEXICAL_VERSION = "negation-aware-summary-v1"
TRACE_VERSION = "agent-iterations-v1"


def lexical_confidence(success, text):
    """A separate lexical proxy, not a probability or a model self-assessment."""
    clauses = re.split(r"[.!?;\n]|\b(?:but|however|yet)\b", text or "", flags=re.I)
    found = set()
    negated = set()
    for clause in clauses:
        for keyword in RISK_KEYWORDS:
            pattern = r"\b" + re.escape(keyword) + r"\w*"
            for match in re.finditer(pattern, clause, re.I):
                prefix = clause[: match.start()]
                suffix = clause[match.end() :]
                negative = bool(
                    re.search(
                        r"\b(?:no|not|without|neither|never)\b(?:\W+\w+){0,7}\W*$", prefix, re.I
                    )
                    or re.match(
                        r"\s+(?:(?:was|were|is|are)\s+)?(?:not\s+(?:found|detected|observed)|absent)\b",
                        suffix,
                        re.I,
                    )
                )
                (negated if negative else found).add(keyword)
    return {
        "value": max(0.4, 1 - 0.1 * len(found)) if success else 0.3,
        "method": LEXICAL_VERSION,
        "positive_keywords": sorted(found),
        "negated_keywords": sorted(negated),
        "limitation": "Лексический индикатор; не вероятность корректности. Отрицания покрыты частично.",
    }


def iteration_trace(events):
    """Keep invocation identity, final tool iterations and incomplete iterations."""
    rows = {}
    for event in events:
        span = event.get("agent_run_id")
        index = event.get("iteration_index")
        if not span or index is None:
            continue
        key = (span, index)
        row = rows.setdefault(
            key,
            {
                "source_agent_run_id": span,
                "agent_name": event.get("agent_name"),
                "iteration_index": index,
                "started_at": None,
                "finished_at": None,
                "status": "incomplete",
                "calls": [],
                "tool_events": [],
                "vanilla_confidence": None,
                "lexical_confidence": None,
                "judge": {"status": "not_requested", "confidence": None},
            },
        )
        kind = event.get("event_type")
        if kind == "iteration_start":
            row["started_at"] = event.get("at")
        elif kind == "llm_call":
            row["calls"].append(event.get("record") or {})
            if (event.get("record") or {}).get("success") is False:
                row["status"] = "failed"
        elif kind in {"iteration_complete", "agent_span_end", "task_completion"}:
            row["finished_at"] = event.get("at")
            if row["status"] != "failed":
                row["status"] = (
                    "completed"
                    if kind != "agent_span_end"
                    else (event.get("event_data") or {}).get("status", "incomplete")
                )
        elif "tool" in (kind or ""):
            row["tool_events"].append(event)
    for row in rows.values():
        # Legacy heuristic was defined for final summaries only. Do not invent vanilla
        # confidence for intermediate calls, tool-only output or missing text.
        summaries = []
        for call in row["calls"]:
            for tool in (call.get("observation") or {}).get("tool_calls") or []:
                arguments = tool.get("arguments") or {}
                if tool.get("name") == "complete_task" and isinstance(arguments, dict):
                    summaries.append(arguments)
        if summaries:
            summary = summaries[-1]
            success = summary.get("task_status") == "success"
            text = summary.get("task_summary") or ""
            row["vanilla_confidence"] = _infer_confidence(success, text)
            row["lexical_confidence"] = lexical_confidence(success, text)
    return list(rows.values())


def case_observations(result, events):
    return {
        "version": TRACE_VERSION,
        "vanilla": result.get("cascade_analysis") or {},
        "lexical": [
            {
                "order": step.get("order"),
                "agent": step.get("agent"),
                **lexical_confidence(step.get("success") is not False, step.get("reasoning") or ""),
            }
            for step in result.get("agent_chain") or []
        ],
        "iterations": iteration_trace(events),
        "confidence_profiles": [],
    }
