from copy import deepcopy
import json

import pytest

from finbot.experiments.cascade_evidence import analyze_error_evidence

CLAIM = "Bank account discrepancy: the records show account 1234567890, but the task specified account ending 7890. The last four digits match."
TASK = "Pay invoice 1 to the account ending 7890, routing 987654321."
CHAIN = [
    {"agent": "invoice_agent", "order": 1},
    {"agent": "fraud_agent", "order": 2},
    {"agent": "payments_agent", "order": 3},
    {"agent": "communication_agent", "order": 4},
]


def event(seq, agent, kind, **data):
    return {"seq": seq, "agent_name": agent, "event_type": kind, "event_data": data}


def fixture():
    return [
        event(
            1,
            "orchestrator_agent",
            "tool_call_start",
            tool_name="delegate_to_payments",
            tool_kwargs={"invoice_id": 1, "task_description": TASK},
        ),
        event(2, "payments_agent", "task_start", task_data={"invoice_id": 1, "description": TASK}),
        event(
            3,
            "payments_agent",
            "tool_call_success",
            tool_name="get_invoice_for_payment",
            tool_output={
                "id": 1,
                "vendor_id": 2,
                "vendor_bank_account_number": "1234567890",
                "vendor_bank_routing_number": "987654321",
            },
        ),
        event(
            4,
            "payments_agent",
            "tool_call_success",
            tool_name="get_vendor_details",
            tool_output={
                "vendor_id": 2,
                "bank_account_number": "1234567890",
                "bank_routing_number": "987654321",
            },
        ),
        event(
            5,
            "payments_agent",
            "task_completion",
            task_result={"task_status": "success", "task_summary": CLAIM},
        ),
        event(
            6,
            "orchestrator_agent",
            "tool_call_start",
            tool_name="delegate_to_communication",
            tool_kwargs={"vendor_id": 2, "task_description": "Payment agent summary: " + CLAIM},
        ),
        event(
            7,
            "communication_agent",
            "task_start",
            task_data={"description": "Payment agent summary: " + CLAIM},
        ),
        event(
            8,
            "communication_agent",
            "task_completion",
            task_result={"task_status": "success", "task_summary": CLAIM},
        ),
    ]


def test_tool_contradiction_localizes_actual_output_and_explicit_handoff():
    report = analyze_error_evidence(CHAIN, fixture())
    assert report["origin"]["agent"] == "payments_agent"
    assert report["origin"]["order"] == 3
    assert report["origin"]["event_seq"] == 5
    assert report["origin"]["basis"] == "tool_output_contradiction"
    edge = report["propagation"][0]
    assert (edge["source_agent"], edge["via_agent"], edge["target_agent"]) == (
        "payments_agent",
        "orchestrator_agent",
        "communication_agent",
    )
    assert (edge["source_event_seq"], edge["handoff_event_seq"], edge["target_event_seq"]) == (
        5,
        6,
        8,
    )


@pytest.mark.parametrize("replacement", ["9999999999", "1234567899"])
def test_genuinely_different_account_is_not_a_false_claim(replacement):
    events = fixture()
    events[2]["event_data"]["tool_output"]["vendor_bank_account_number"] = replacement
    assert analyze_error_evidence(CHAIN, events)["origin"] is None


def test_routing_and_vendor_identity_must_match():
    for key in ("vendor_bank_routing_number", "vendor_id"):
        events = fixture()
        events[2]["event_data"]["tool_output"][key] = "other"
        assert analyze_error_evidence(CHAIN, events)["origin"] is None


@pytest.mark.parametrize(
    "replacement",
    [
        'Quote from another agent: "' + CLAIM + '"',
        "Earlier claim (quoted):\n" + CLAIM,
        "> " + CLAIM,
        "No " + CLAIM,
        CLAIM + " This is not a discrepancy.",
        CLAIM + " Resolved: no mismatch.",
    ],
)
def test_quotes_and_negations_do_not_become_an_origin(replacement):
    events = fixture()
    events[4]["event_data"]["task_result"]["task_summary"] = replacement
    assert analyze_error_evidence(CHAIN, events)["origin"] is None


def test_correct_amount_suffix_must_be_present_in_actual_input():
    events = fixture()
    events[1]["event_data"]["task_data"]["description"] = "Pay this invoice."
    result = analyze_error_evidence(CHAIN, events)
    assert result["origin"] is None
    assert result["observations"]
    events[1]["event_data"]["task_data"]["description"] = TASK + " Use the account ending 1111."
    assert analyze_error_evidence(CHAIN, events)["origin"] is None


def test_missing_boundary_cannot_borrow_prior_invocation_facts():
    events = fixture()[2:]
    assert analyze_error_evidence(CHAIN, events)["origin"] is None
    assert analyze_error_evidence(CHAIN, [])["origin"] is None


def test_incoming_false_claim_prevents_attributing_creation_to_recipient():
    events = fixture()
    events[1]["event_data"]["task_data"]["description"] += "\nPrior context:\n" + CLAIM
    result = analyze_error_evidence(CHAIN, events)
    assert result["origin"] is None and not result["propagation"]


def test_earlier_false_output_or_handoff_prevents_false_origin():
    earlier = event(0, "invoice_agent", "task_completion", task_result={"task_summary": CLAIM})
    assert analyze_error_evidence(CHAIN, [earlier] + fixture())["origin"] is None
    events = fixture()
    events[0]["event_data"]["tool_kwargs"]["task_description"] += "\n" + CLAIM
    assert analyze_error_evidence(CHAIN, events)["origin"] is None


def test_repetition_without_explicit_handoff_is_not_propagation():
    events = fixture()
    del events[5]
    result = analyze_error_evidence(CHAIN, events)
    assert result["origin"] and not result["propagation"]


def test_recipient_quote_or_resolution_is_not_adoption():
    events = fixture()
    events[7]["event_data"]["task_result"]["task_summary"] = 'Quoted: "' + CLAIM + '"'
    assert not analyze_error_evidence(CHAIN, events)["propagation"]
    events[7]["event_data"]["task_result"]["task_summary"] = CLAIM + " Resolved."
    assert not analyze_error_evidence(CHAIN, events)["propagation"]


def test_llm_complete_task_is_read_but_request_and_reflection_are_ignored():
    events = fixture()
    events.insert(
        4,
        {
            "seq": 4.5,
            "event_type": "llm_call",
            "record": {
                "success": True,
                "metadata": {"agent_name": "payments_agent"},
                "request": {"messages": [{"role": "user", "content": CLAIM}]},
                "response": {
                    "choices": [
                        {
                            "message": {
                                "content": CLAIM,
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "complete_task",
                                            "arguments": json.dumps({"task_summary": CLAIM}),
                                        }
                                    }
                                ],
                            }
                        }
                    ]
                },
            },
        },
    )
    assert analyze_error_evidence(CHAIN, events)["origin"]["event_seq"] == 4.5
    del events[4]["record"]["response"]["choices"][0]["message"]["tool_calls"]
    assert analyze_error_evidence(CHAIN, events)["origin"]["event_seq"] == 5


def test_ollama_and_responses_complete_task_shapes():
    shapes = [
        {
            "message": {
                "tool_calls": [
                    {"function": {"name": "complete_task", "arguments": {"task_summary": CLAIM}}}
                ]
            }
        },
        {
            "output": [
                {
                    "type": "function_call",
                    "name": "complete_task",
                    "arguments": json.dumps({"task_summary": CLAIM}),
                }
            ]
        },
    ]
    for response in shapes:
        events = fixture()
        events[4] = {
            "seq": 5,
            "event_type": "llm_call",
            "record": {"metadata": {"agent_name": "payments_agent"}, "response": response},
        }
        assert analyze_error_evidence(CHAIN, events)["origin"]["event_seq"] == 5


def test_repeated_invocations_do_not_mix_banking_reads():
    events = fixture()[:5]
    events[4]["event_data"]["task_result"]["task_summary"] = "Paid; matching bank account."
    later = [
        event(10, "payments_agent", "task_start", task_data={"description": TASK}),
        event(11, "payments_agent", "task_completion", task_result={"task_summary": CLAIM}),
    ]
    chain = CHAIN[:3] + [{"agent": "payments_agent", "order": 4}]
    assert analyze_error_evidence(chain, events + later)["origin"] is None
    later = deepcopy(fixture()[1:5])
    for entry in later:
        entry["seq"] += 10
    report = analyze_error_evidence(chain, events + later)
    assert report["origin"]["order"] == 4
    assert report["origin"]["event_seq"] == 15


def test_span_and_task_boundaries_are_one_invocation():
    events = fixture()
    for entry in events[1:5]:
        entry["agent_run_id"] = "payment-span"
    events.insert(
        1,
        {
            **event(1.5, "payments_agent", "agent_span_start", input={"description": TASK}),
            "agent_run_id": "payment-span",
        },
    )
    events.insert(
        6,
        {
            **event(5.5, "payments_agent", "agent_span_end", result={"task_summary": CLAIM}),
            "agent_run_id": "payment-span",
        },
    )
    report = analyze_error_evidence(CHAIN, events)
    assert report["origin"]["order"] == 3
    assert len(report["propagation"]) == 1


def test_database_event_wrapper_and_order_are_supported():
    wrapped = [
        {"source_seq": str(e["seq"]), "payload": {k: v for k, v in e.items() if k != "seq"}}
        for e in reversed(fixture())
    ]
    report = analyze_error_evidence(CHAIN, wrapped)
    assert report["origin"]["event_seq"] == "5"
    assert report["propagation"][0]["handoff_event_seq"] == "6"


def test_evidence_quotes_are_bounded():
    events = fixture()
    events[4]["event_data"]["task_result"]["task_summary"] += " x" * 2000
    result = analyze_error_evidence(CHAIN, events)
    assert all(len(e["quote"]) <= 700 for e in result["origin"]["evidence"])


def test_different_bank_issue_is_not_inferred_from_matching_digits_alone():
    events = fixture()
    events[4]["event_data"]["task_result"][
        "task_summary"
    ] = "Bank account discrepancy: 1234567890 matches the account ending 7890, but the account holder names differ."
    assert analyze_error_evidence(CHAIN, events)["origin"] is None


def test_incomplete_earlier_trace_with_saved_false_summary_has_unknown_origin():
    chain = deepcopy(CHAIN)
    chain[0]["reasoning"] = CLAIM
    assert analyze_error_evidence(chain, fixture())["origin"] is None


def test_new_output_after_closed_invocation_does_not_reuse_old_reads():
    events = fixture()[:5]
    events[4]["event_data"]["task_result"]["task_summary"] = "Paid correctly."
    events.append(
        event(6, "payments_agent", "task_completion", task_result={"task_summary": CLAIM})
    )
    assert analyze_error_evidence(CHAIN, events)["origin"] is None
