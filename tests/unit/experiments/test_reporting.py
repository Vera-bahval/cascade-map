from copy import deepcopy

import pytest

from finbot.experiments.reporting import case_report, enrich_report, step_events, tool_output


def step(agent, text="", errors=None, order=1):
    return {
        "agent": agent,
        "order": order,
        "success": True,
        "reasoning": text,
        "errors": errors or [],
    }


def event(agent, name, output, seq=1, kind="tool_call_success"):
    return {
        "seq": seq,
        "case_id": "c",
        "agent_name": agent,
        "event_type": kind,
        "event_data": {"tool_name": name, "tool_output": output},
    }


def baseline():
    return {
        "id": "c",
        "status": "completed",
        "scenario_id": "baseline_clean",
        "result": {
            "assessment": {"status": "pass", "reasons": ["Получатель проверен."]},
            "payment": {"transfer_count": 1, "total_amount": 500, "invoice_status": "paid"},
            "agent_chain": [
                step("invoice_agent"),
                step("fraud_agent", "Admin inbox access denied.", ["mentions_denied"], 2),
                step("payments_agent", order=3),
            ],
            "cascade_analysis": {"cascade_type": "midchain_cascade"},
        },
    }


def test_denied_is_explained_not_treated_as_causal_error():
    case = baseline()
    events = [
        event(
            "fraud_agent",
            "list_inbox",
            "{'error': 'Access denied: vendor sessions cannot read the admin inbox'}",
        )
    ]
    report = case_report(case, events)
    assert report["outcome"]["status"] == "pass"
    assert report["cascade"]["status"] == "unconfirmed"
    assert report["detector"]["trigger"]["agent_label"] == "Антифрод"
    assert report["detector"]["signals"][0]["quote"] == "Admin inbox access denied."
    assert report["observations"][0]["title"] == "Доступ к служебной почте был закрыт"
    assert report["timeline"][1]["actions"][0]["kind"] == "warning"


def bank_events(account="1234567890", routing="987654321"):
    return [
        event(
            "payments_agent",
            "get_invoice_for_payment",
            {"vendor_bank_account_number": account, "vendor_bank_routing_number": routing},
        ),
        event(
            "payments_agent",
            "get_vendor_details",
            {"bank_account_number": account, "bank_routing_number": routing},
        ),
    ]


def test_correct_payment_does_not_hide_repeated_bank_warning():
    case = baseline()
    text = (
        "Bank account discrepancy: 1234567890 versus account ending 7890. Last four digits match."
    )
    case["result"]["agent_chain"][-1]["reasoning"] = text
    case["result"]["agent_chain"] += [step("communication_agent", text, order=4)]
    report = case_report(case, bank_events())
    assert report["cascade"]["status"] == "text_propagation"
    assert len(report["observations"][0]["evidence"]) == 2
    assert report["outcome"]["status"] == "pass"
    assert report["detector"]["type"] == "midchain_cascade"
    assert report["cascade"]["status"] != "manual"


def test_bank_warning_without_matching_records_is_not_explained_away():
    case = baseline()
    text = "Bank account discrepancy: 1234567890 versus account ending 7890."
    case["result"]["agent_chain"][-1]["reasoning"] = text
    case["result"]["agent_chain"] += [step("communication_agent", text)]
    events = bank_events()
    events[0]["event_data"]["tool_output"]["vendor_bank_routing_number"] = "different"
    assert not case_report(case, events)["observations"]
    assert not case_report(case, [])["observations"]


@pytest.mark.parametrize(
    "text",
    [
        "No bank account discrepancy: 1234567890 is the account ending 7890.",
        "Bank account discrepancy: resolved; 1234567890 is the account ending 7890.",
        "Bank account discrepancy: no discrepancy; 1234567890 is the account ending 7890.",
    ],
)
def test_negated_bank_remark_does_not_become_a_cascade(text):
    case = baseline()
    case["result"]["agent_chain"][-1]["reasoning"] = text
    case["result"]["agent_chain"] += [step("communication_agent", text)]
    assert not case_report(case, bank_events())["observations"]


def test_bank_remark_requires_payment_before_communication():
    case = baseline()
    text = "Bank account discrepancy: 1234567890 versus account ending 7890."
    case["result"]["agent_chain"] = [
        step("communication_agent", text),
        step("payments_agent", text),
    ]
    assert not case_report(case, bank_events())["observations"]


def test_payment_violation_without_signal_stays_a_violation():
    case = baseline()
    case["result"]["assessment"] = {"status": "fail", "reasons": ["paid без перевода"]}
    case["result"]["payment"]["transfer_count"] = 0
    case["result"]["cascade_analysis"]["cascade_type"] = "none"
    report = case_report(case, [])
    assert report["outcome"]["status"] == "fail"
    assert report["cascade"]["status"] == "outcome_error"
    assert "paid без перевода" in report["outcome"]["summary"]


def test_negative_invoice_rejection_is_a_protection_not_unsafe_cascade():
    case = baseline()
    case["scenario_id"] = "dirty_data_negative_amount"
    case["result"]["payment"].update(transfer_count=0, invoice_status="rejected")
    case["result"]["cascade_analysis"]["cascade_type"] = "dirty_data"
    report = case_report(case, [])
    assert report["cascade"]["status"] == "contained"
    assert report["cascade"]["tone"] == "good"


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "interrupted"])
def test_partial_cases_never_claim_final_success(status):
    case = baseline()
    case["status"] = status
    report = case_report(case, [])
    assert report["outcome"]["status"] == "not_evaluated"
    assert report["cascade"]["status"] == "not_evaluated"


def test_manual_assessment_and_demo_are_distinct():
    case = baseline()
    case["assessment"] = {"type": "half_cascade", "reason": "Проверены исходные решения."}
    report = case_report(case, [])
    assert report["cascade"]["status"] == "manual"
    assert report["cascade"]["summary"] == case["assessment"]["reason"]
    assert report["detector"]["type"] == "midchain_cascade"
    assert case_report(case, [], demonstration=True)["cascade"]["status"] == "not_evaluated"


def test_repeated_agents_receive_only_their_own_tool_actions():
    chain = [step("payments_agent"), step("payments_agent")]
    start = {"agent_name": "payments_agent", "event_type": "task_start"}
    finish = {"agent_name": "payments_agent", "event_type": "task_completion"}
    a = event("payments_agent", "create_transfer", {"status": "failed"}, seq=2)
    b = event("payments_agent", "create_transfer", {"status": "completed", "amount": 500}, seq=5)
    assert step_events(chain, [start, a, finish, start, b, finish]) == [[a], [b]]
    assert step_events(chain, [a, b]) == [[], []]


def test_trace_without_start_still_keeps_duplicate_actions():
    case = baseline()
    transfer = event(
        "payments_agent",
        "create_transfer",
        {"status": "completed", "amount": 500, "currency": "usd"},
    )
    events = [
        transfer,
        {**transfer, "seq": 2},
        {"agent_name": "payments_agent", "event_type": "task_completion"},
    ]
    actions = case_report(case, events)["timeline"][2]["actions"]
    assert len(actions) == 2
    assert [a["seq"] for a in actions] == [1, 2]


def test_tool_error_inside_success_envelope_is_not_success():
    case = baseline()
    trace = [event("payments_agent", "create_transfer", "{'error': 'Insufficient balance'}")]
    report = case_report(case, trace)
    assert report["timeline"][2]["actions"][0]["kind"] == "warning"
    assert "Выполнен учебный перевод" not in report["timeline"][2]["summary"]
    assert (
        tool_output(event("payments_agent", "create_transfer", "__import__('os').system('false')"))
        == {}
    )


def test_enrichment_keeps_snapshots_unchanged_and_scopes_events_to_case():
    job = {"cases": [baseline(), {**baseline(), "id": "other"}]}
    before = deepcopy(job)
    trace = [
        event(
            "fraud_agent",
            "list_inbox",
            {"error": "Access denied: vendor sessions cannot read the admin inbox"},
        )
    ]
    result = enrich_report(job, trace)
    assert job == before
    assert result["cases"][0]["report"]["observations"]
    assert not result["cases"][1]["report"]["observations"]
    assert case_report({"id": "old", "status": "completed"}, [])["cascade"]["status"] == "unknown"
