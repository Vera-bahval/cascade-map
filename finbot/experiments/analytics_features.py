"""Descriptive trace features, with explicit measurement coverage."""

import json
import re

from finbot.experiments.authority_evidence import _AUTHORITY, _adoption

READ_TOOLS = {"get_vendor_details", "get_vendor_risk_profile", "get_invoice_details"}
WRITE_TOOLS = {"update_invoice_status", "process_payment"}


def trace_features(case, agents_filter=()):
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
