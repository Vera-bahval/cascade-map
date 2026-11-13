"""Descriptive trace features, with explicit measurement coverage."""

import json
import re

from finbot.experiments.authority_evidence import _AUTHORITY, _adoption

READ_TOOLS = {"get_vendor_details", "get_vendor_risk_profile", "get_invoice_details"}
WRITE_TOOLS = {"update_invoice_status", "process_payment"}


def _legacy_trace_features(case, agents_filter=()):
    agents = [
        a for a in case.get("agents", []) if not agents_filter or a["agent_name"] in agents_filter
    ]
    claims = []
    for agent in agents:
        summary = agent.get("summary")
        if not summary:
            continue
        if _AUTHORITY.search(summary):
            claims.append(
                {
                    "agent": agent["agent_name"],
                    "state": "asserted" if _adoption(summary) else "qualified_or_attributed",
                }
            )
    tools = case.get("tool_events") or []
    lookups = []
    for agent in agents:
        if (agent.get("provenance") or {}).get("identity") != "explicit_span":
            continue
        own = [e for e in tools if e.get("agent_run_id") == agent["id"]]
        own.sort(
            key=lambda e: int(e["source_seq"]) if str(e.get("source_seq", "")).isdigit() else 0
        )
        reads = 0
        for event in own:
            name = (event.get("payload", {}).get("event_data") or {}).get("tool_name")
            if name in WRITE_TOOLS:
                lookups.append(
                    {
                        "agent": agent["agent_name"],
                        "reads_before_write": reads,
                        "decision_without_lookup": reads == 0,
                    }
                )
                break
            if name in READ_TOOLS:
                reads += 1
    # Literal trigram recall, not semantic adoption or an estimate of token counts.
    ordered = sorted(
        [a for a in case.get("agents", []) if a.get("step_order") is not None],
        key=lambda a: a["step_order"],
    )
    handoffs = []

    def trigrams(text):
        words = re.findall(r"\w+", text.casefold())
        return set(zip(words, words[1:], words[2:]))

    for first, second in zip(ordered, ordered[1:]):
        if agents_filter and second["agent_name"] not in agents_filter:
            continue
        summary, context = first.get("summary"), second.get("input")
        if not summary or not context:
            continue
        grams = trigrams(summary)
        if grams:
            handoffs.append(
                {
                    "upstream": first["agent_name"],
                    "downstream": second["agent_name"],
                    "trigram_recall": len(grams & trigrams(json.dumps(context, ensure_ascii=False)))
                    / len(grams),
                }
            )
    return {"authority_mentions": claims, "verification": lookups, "handoffs": handoffs}


VERIFICATION_READS = READ_TOOLS | {
    "get_invoice_for_payment",
    "get_vendor_invoice_history",
    "get_vendor_invoices",
    "get_vendor_payment_summary",
    "get_payment_history",
}
DECISION_WRITES = WRITE_TOOLS | {"create_transfer", "flag_invoice_for_review"}


def trace_features(case, agents_filter=()):
    from finbot.experiments.reporting import tool_output
    from finbot.experiments.research_outcomes import manual_review

    legacy = _legacy_trace_features(case, agents_filter)
    agents = [
        a for a in case.get("agents", []) if not agents_filter or a["agent_name"] in agents_filter
    ]
    verification = []
    for agent in agents:
        if (agent.get("provenance") or {}).get("identity") != "explicit_span":
            continue
        own = [e for e in case.get("tool_events", []) if e.get("agent_run_id") == agent["id"]]
        if not own or any(not str(e.get("source_seq", "")).isdigit() for e in own):
            continue
        own.sort(key=lambda e: int(e["source_seq"]))
        attempted, completed, successful = 0, 0, 0
        for event in own:
            payload = event.get("payload") or {}
            kind = event.get("event_type") or payload.get("event_type")
            name = str((payload.get("event_data") or {}).get("tool_name") or "").rsplit("__", 1)[-1]
            if name in DECISION_WRITES:
                complete = attempted == completed
                verification.append(
                    {
                        "agent": agent["agent_name"],
                        "agent_ref": agent.get("source_agent_run_id"),
                        "decision_tool": name,
                        "read_attempts_before_write": attempted,
                        "known_successful_reads": successful,
                        "read_outcomes_complete": complete,
                        "reads_before_write": successful if complete else None,
                        "decision_without_lookup": successful == 0 if complete else None,
                    }
                )
                break
            if name not in VERIFICATION_READS:
                continue
            if kind == "tool_call_start":
                attempted += 1
            elif kind in {
                "tool_call_success",
                "mcp_tool_call_success",
                "tool_call_failure",
                "mcp_tool_call_failure",
            }:
                completed += 1
                output = tool_output(payload)
                if (
                    kind.endswith("success")
                    and output
                    and not output.get("error")
                    and output.get("success") is not False
                ):
                    successful += 1
    ordered = sorted(
        [a for a in case.get("agents", []) if a.get("step_order") is not None],
        key=lambda a: a["step_order"],
    )
    handoffs = []

    def grams(text):
        words = re.findall(r"\w+", text.casefold())
        return set(zip(words, words[1:], words[2:]))

    def normalize(text):
        return " ".join(str(text).casefold().split())

    for index, target in enumerate(ordered):
        if agents_filter and target["agent_name"] not in agents_filter:
            continue
        context = target.get("input")
        if not context:
            continue
        text = json.dumps(context, ensure_ascii=False)
        # Every prior summary is a candidate, not only the immediately preceding role.
        for source in ordered[:index]:
            summary = source.get("summary")
            if not summary or source["step_order"] >= target["step_order"]:
                continue
            source_grams = grams(summary)
            if not source_grams:
                continue
            strings = []

            def strings_in(value):
                if isinstance(value, str):
                    strings.append(value)
                elif isinstance(value, dict):
                    for child in value.values():
                        strings_in(child)
                elif isinstance(value, list):
                    for child in value:
                        strings_in(child)

            strings_in(context)
            literal = any(normalize(summary) in normalize(s) for s in strings)
            handoffs.append(
                {
                    "upstream": source["agent_name"],
                    "downstream": target["agent_name"],
                    "source_agent_ref": source.get("source_agent_run_id"),
                    "agent_ref": target.get("source_agent_run_id"),
                    "trigram_recall": len(source_grams & grams(text)) / len(source_grams),
                    "literal_summary_present": literal,
                    "summary_characters": len(summary),
                    "target_input_characters": len(text),
                    "assertion_transition_candidate": bool(
                        literal
                        and _AUTHORITY.search(summary)
                        and not _adoption(summary)
                        and _adoption(target.get("summary") or "")
                    ),
                }
            )
    claims = manual_review(case).get("claims") or []
    if agents_filter:
        refs = {a.get("source_agent_run_id") for a in agents}
        claims = [c for c in claims if c["agent_ref"] in refs]
    return {
        "version": "trace-features-v2",
        "authority_mentions": legacy["authority_mentions"],
        "verification": verification,
        "handoffs": handoffs,
        "reviewed_claims": claims,
        "legacy_verification": legacy["verification"],
        "legacy_handoffs": legacy["handoffs"],
    }
