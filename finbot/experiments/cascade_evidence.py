"""Conservative error localization from saved outputs and explicit handoffs."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

DETECTOR_VERSION = "trace-evidence-2026-11-03-v2"
_LABELS = {
    "invoice_agent": "Проверка счёта",
    "fraud_agent": "Антифрод",
    "payments_agent": "Платёжный агент",
    "communication_agent": "Уведомления",
    "orchestrator_agent": "Оркестратор",
}
_HEADING = re.compile(
    r"(?:bank[ -]account\s+(?:discrepancy|mismatch)|discrepancy to note)\s*:", re.I
)
_NEGATION = re.compile(
    r"\b(?:no discrepancy|not a discrepancy|no mismatch|resolved|false alarm|"
    r"incorrect claim|incorrect statement|does not exist|not true|not an issue)\b",
    re.I,
)
_QUOTE = re.compile(
    r"\b(?:quoted?|verbatim|example|hypothetical|earlier claim|previous claim|prior claim)\b", re.I
)


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= 100_000:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _description(value):
    value = _object(value)
    return str(value.get("description") or value.get("task_description") or "")


def _agent(event):
    metadata = (event.get("record") or {}).get("metadata") or {}
    return event.get("agent_name") or metadata.get("agent_name")


def _span(event):
    metadata = (event.get("record") or {}).get("metadata") or {}
    return event.get("agent_run_id") or metadata.get("agent_run_id")


def _events(events):
    normalized = []
    for source in events:
        event = dict(source.get("payload") or source)
        event.setdefault("seq", source.get("source_seq"))
        normalized.append(event)
    if normalized and all(str(e.get("seq", "")).isdigit() for e in normalized):
        normalized.sort(key=lambda e: int(e["seq"]))
    return normalized


def _summaries(event):
    data, kind = event.get("event_data") or {}, event.get("event_type")
    if kind == "task_completion":
        result = _object(data.get("task_result"))
    elif kind == "agent_span_end":
        result = _object(data.get("result"))
    elif kind == "tool_call_start" and data.get("tool_name") == "complete_task":
        result = _object(data.get("tool_kwargs") or data.get("tool_arguments"))
    else:
        result = {}
    if isinstance(result.get("task_summary"), str):
        yield result["task_summary"]
    if kind != "llm_call" or (event.get("record") or {}).get("success") is False:
        return
    response = _object((event.get("record") or {}).get("response"))
    messages = [
        c.get("message") or {} for c in response.get("choices") or [] if isinstance(c, dict)
    ]
    if isinstance(response.get("message"), dict):
        messages.append(response["message"])
    calls = [call for message in messages for call in message.get("tool_calls") or []]
    calls.extend(
        c
        for c in response.get("output") or []
        if isinstance(c, dict) and c.get("type") == "function_call"
    )
    for call in calls:
        function = call.get("function") or call
        if function.get("name") == "complete_task":
            result = _object(function.get("arguments"))
            if isinstance(result.get("task_summary"), str):
                yield result["task_summary"]


def _claim(text, account, *, quoted=False):
    """Recognize an asserted specific contradiction, never arbitrary prompt text."""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.search(line)
        if not match:
            continue
        prefix = line[: match.start()].strip()
        if not quoted:
            if prefix and not re.fullmatch(r"[-*•]?\s*(?:\d+\.)?", prefix):
                continue
            previous = next((x.strip() for x in reversed(lines[:index]) if x.strip()), "")
            if _QUOTE.search(previous) or previous.endswith(('"', "“", "```")):
                continue
        passage = line[match.start() :].strip()
        if _NEGATION.search(passage):
            continue
        suffix = re.search(
            r"\b(?:account\s+(?:ending(?:\s+in)?|ends?\s+in)|(?:the\s+)?one\s+ending)\s*"
            + re.escape(account[-4:])
            + r"\b",
            passage,
            re.I,
        )
        full = re.search(r"(?<!\d)" + re.escape(account) + r"(?!\d)", passage)
        if full and suffix and full.end() < suffix.start():
            relation = passage[full.end() : suffix.start()]
            if (
                len(relation) <= 150
                and re.search(r"\b(?:not|but|versus|vs|different|rather than)\b", relation, re.I)
                and not re.search(r"\b(?:matches|same|identical)\b", relation, re.I)
            ):
                return passage[:700]
    return None


def _correct_suffix(text, account):
    suffixes = re.findall(r"\baccount\s+ending(?:\s+in)?\s*(\d{4})\b", text, re.I)
    return bool(suffixes) and set(suffixes) == {account[-4:]}


@dataclass
class _Invocation:
    agent: str
    index: int
    order: int | None
    span: str | None = None
    task: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    closed: bool = False
    ambiguous: bool = False


def _invocations(chain, events):
    orders = defaultdict(deque)
    for step in chain:
        orders[step.get("agent")].append(step.get("order"))
    invocations, active, spans, handoffs = [], {}, {}, []
    for index, event in enumerate(events):
        agent, span, kind = _agent(event), _span(event), event.get("event_type")
        data = event.get("event_data") or {}
        if not agent:
            continue
        current = spans.get(span) if span else active.get(agent)
        if kind in {"task_start", "agent_span_start"}:
            if kind == "task_start" and current and current.span and not current.closed:
                pass
            else:
                ambiguous = bool(current and not current.closed)
                if ambiguous:
                    current.ambiguous = True
                current = _Invocation(
                    agent,
                    index,
                    orders[agent].popleft() if orders[agent] else None,
                    span=span,
                    ambiguous=ambiguous,
                )
                invocations.append(current)
                active[agent] = current
                if span:
                    spans[span] = current
            incoming = _object(data.get("task_data") if kind == "task_start" else data.get("input"))
            if incoming:
                current.task = incoming
        if kind == "tool_call_start" and str(data.get("tool_name", "")).startswith("delegate_to_"):
            target = data["tool_name"].removeprefix("delegate_to_") + "_agent"
            args = _object(data.get("tool_kwargs") or data.get("tool_arguments"))
            handoffs.append((index, event, target, _description(args)))
        summaries = list(_summaries(event))
        if (
            current
            and current.closed
            and kind != "agent_span_end"
            and any(not any(old[2] == text for old in current.outputs) for text in summaries)
        ):
            current = None
        if current is None:
            # Incomplete exports may retain outputs without the invocation start.
            current = _Invocation(agent, index, None, span=span, ambiguous=True)
            invocations.append(current)
            active[agent] = current
            if span:
                spans[span] = current
        current.events.append((index, event))
        for text in summaries:
            if not any(old[2] == text for old in current.outputs):
                current.outputs.append((index, event, text))
        if kind in {"task_completion", "agent_span_end"}:
            current.closed = True
    return invocations, handoffs


def _facts(invocation, before):
    invoice, vendor, references = {}, {}, {}
    for index, event in invocation.events:
        if index >= before or event.get("event_type") not in {
            "tool_call_success",
            "mcp_tool_call_success",
        }:
            continue
        data = event.get("event_data") or {}
        output = _object(data.get("tool_output"))
        if output.get("error"):
            continue
        if data.get("tool_name") == "get_invoice_for_payment":
            invoice, references["invoice"] = output, event.get("seq")
        elif data.get("tool_name") == "get_vendor_details":
            vendor, references["vendor"] = output, event.get("seq")
    account = str(vendor.get("bank_account_number") or "")
    if (
        not account.isdigit()
        or len(account) <= 4
        or invoice.get("vendor_bank_account_number") != account
    ):
        return None
    routing = vendor.get("bank_routing_number")
    if not routing or invoice.get("vendor_bank_routing_number") != routing:
        return None
    if (
        invoice.get("vendor_id") is not None
        and vendor.get("vendor_id") is not None
        and invoice["vendor_id"] != vendor["vendor_id"]
    ):
        return None
    if (
        invocation.task.get("invoice_id") is not None
        and invoice.get("id") is not None
        and invocation.task["invoice_id"] != invoice["id"]
    ):
        return None
    references["invoice_quote"] = json.dumps(
        {key: invoice[key] for key in ("vendor_bank_account_number", "vendor_bank_routing_number")}
    )
    references["vendor_quote"] = json.dumps(
        {key: vendor[key] for key in ("bank_account_number", "bank_routing_number")}
    )
    return account, references


def _quote(agent, seq, quote):
    return {
        "agent": agent,
        "agent_label": _LABELS.get(agent, agent),
        "event_seq": seq,
        "quote": quote[:700],
    }


def _analyze_bank_evidence(chain, events):
    """Locate only contradictions supported by facts and explicit trace boundaries."""
    result = {
        "origin": None,
        "propagation": [],
        "observations": [],
        "detector_version": DETECTOR_VERSION,
    }
    events = _events(events)
    invocations, handoffs = _invocations(chain, events)
    outputs = sorted(
        (
            (index, event, text, invocation)
            for invocation in invocations
            for index, event, text in invocation.outputs
        ),
        key=lambda row: row[0],
    )
    selected = None
    for index, event, text, invocation in outputs:
        facts = _facts(invocation, index)
        if not facts:
            continue
        account, refs = facts
        claim = _claim(text, account)
        if not claim:
            continue
        evidence = [
            _quote(
                invocation.agent,
                refs["invoice"],
                refs["invoice_quote"],
            ),
            _quote(
                invocation.agent,
                refs["vendor"],
                refs["vendor_quote"],
            ),
            _quote(invocation.agent, event.get("seq"), claim),
        ]
        task = _description(invocation.task)
        prior = any(
            _claim(old_text, account, quoted=True)
            for old_index, _, old_text, _ in outputs
            if old_index < index
        )
        incoming = _claim(task, account, quoted=True)
        prior_handoff = any(
            _claim(description, account, quoted=True)
            for old_index, _, _, description in handoffs
            if old_index < invocation.index
        )
        prior_chain = any(
            isinstance(step.get("order"), int)
            and isinstance(invocation.order, int)
            and step["order"] < invocation.order
            and _claim(step.get("reasoning"), account, quoted=True)
            for step in chain
        )
        if (
            invocation.ambiguous
            or not _correct_suffix(task, account)
            or prior
            or incoming
            or prior_handoff
            or prior_chain
        ):
            result["observations"].append(
                {
                    "title": "Замечание о реквизитах противоречит данным инструментов",
                    "detail": "Полный номер счёта совпадает с указанным окончанием. Источник ошибочного замечания не установлен: входная трасса неполна или оно уже присутствовало раньше.",
                    "tone": "warning",
                    "evidence": evidence,
                }
            )
            continue
        origin = {
            "agent": invocation.agent,
            "order": invocation.order,
            "event_seq": event.get("seq"),
            "code": "bank_account_false_mismatch",
            "basis": "tool_output_contradiction",
            "summary": "В итоговом ответе полный номер счёта ошибочно назван несовпадающим с его последними четырьмя цифрами. Прочитанные реквизиты совпадают.",
            "evidence": evidence,
        }
        result["origin"] = origin
        result["observations"].append(
            {
                "title": "Найден источник ложного замечания о реквизитах",
                "detail": origin["summary"],
                "tone": "warning",
                "evidence": evidence,
            }
        )
        selected = (index, event, text, invocation, account)
        break
    if not selected:
        result["observations"] = result["observations"][:1]
        return result
    origin_index, origin_event, _, origin_invocation, account = selected
    known = [(origin_index, origin_event, origin_invocation, selected[2])]
    for handoff_index, handoff_event, target, description in handoffs:
        handoff_claim = _claim(description, account, quoted=True)
        if handoff_index <= origin_index or not handoff_claim:
            continue
        candidates = [
            inv for inv in invocations if inv.agent == target and inv.index > handoff_index
        ]
        if not candidates:
            continue
        target_inv = min(candidates, key=lambda inv: inv.index)
        if target_inv.ambiguous or not _claim(_description(target_inv.task), account, quoted=True):
            continue
        if any(
            handoff_index < other[0] < target_inv.index and other[2] == target for other in handoffs
        ):
            continue
        target_output = next(
            ((idx, ev, txt) for idx, ev, txt in target_inv.outputs if _claim(txt, account)), None
        )
        sources = [
            source
            for source in known
            if source[0] < handoff_index
            and _claim(source[3], account).lower() in description.lower()
        ]
        if not target_output or not sources:
            continue
        source_index, source_event, source_inv, _ = max(sources, key=lambda x: x[0])
        target_index, target_event, target_text = target_output
        edge = {
            "source_agent": source_inv.agent,
            "target_agent": target,
            "via_agent": _agent(handoff_event),
            "source_event_seq": source_event.get("seq"),
            "handoff_event_seq": handoff_event.get("seq"),
            "target_event_seq": target_event.get("seq"),
            "source_order": source_inv.order,
            "target_order": target_inv.order,
            "evidence": [
                _quote(_agent(handoff_event), handoff_event.get("seq"), handoff_claim),
                _quote(target, target_event.get("seq"), _claim(target_text, account)),
            ],
        }
        result["propagation"].append(edge)
        known.append((target_index, target_event, target_inv, target_text))
        result["observations"].append(
            {
                "title": "Ложный вывод передан следующему агенту",
                "detail": f"{_LABELS.get(source_inv.agent, source_inv.agent)} → {_LABELS.get(_agent(handoff_event), _agent(handoff_event))} → {_LABELS.get(target, target)}: замечание явно передано в задании и повторено в итоговом ответе.",
                "tone": "warning",
                "evidence": edge["evidence"],
            }
        )
    return result


def analyze_error_evidence(chain, events, scenario_id=None):
    from finbot.experiments.authority_evidence import analyze_authority_evidence

    authority = analyze_authority_evidence(chain, events, scenario_id)
    bank = _analyze_bank_evidence(chain, events)
    if authority:
        bank_origin = bank.get("origin")
        first = authority["origin"].get("event_seq")
        other = (bank_origin or {}).get("event_seq")
        if not bank_origin or (
            str(first).isdigit() and str(other).isdigit() and int(first) < int(other)
        ):
            return {**bank, **authority}
    return bank
