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
    assert report["cascade"]["status"] == "no_signal"
    assert report["detector"]["trigger"] is None
    assert report["detector"]["signals"] == []
    assert report["detector"]["original"]["type"] == "midchain_cascade"
    assert report["error_location"]["status"] == "unknown"
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
    assert report["cascade"]["status"] == "unconfirmed"
    assert len(report["observations"][0]["evidence"]) == 2
    assert report["outcome"]["status"] == "pass"
    assert report["detector"]["type"] == "needs_review"
    assert report["error_location"]["status"] == "unknown"
    assert report["cascade"]["status"] != "manual"


def test_evidence_locates_summary_error_and_explicit_handoff_after_correct_payment():
    case = baseline()
    text = (
        "Bank account discrepancy: 1234567890 versus account ending 7890. Last four digits match."
    )
    case["result"]["agent_chain"][-1]["reasoning"] = text
    case["result"]["agent_chain"].append(step("communication_agent", text, order=4))
    before = deepcopy(case)

    def trace(seq, agent, kind, data):
        return {
            "seq": seq,
            "case_id": "c",
            "agent_name": agent,
            "event_type": kind,
            "event_data": data,
        }

    incoming = "Pay invoice 1 to account ending 7890, routing 987654321."
    events = [
        trace(
            1,
            "orchestrator_agent",
            "tool_call_start",
            {"tool_name": "delegate_to_payments", "tool_kwargs": {"task_description": incoming}},
        ),
        trace(2, "payments_agent", "task_start", {"task_data": {"description": incoming}}),
        {**bank_events()[0], "seq": 3},
        {**bank_events()[1], "seq": 4},
        trace(
            5,
            "payments_agent",
            "task_completion",
            {"task_result": {"task_status": "success", "task_summary": text}},
        ),
        trace(
            6,
            "orchestrator_agent",
            "tool_call_start",
            {
                "tool_name": "delegate_to_communication",
                "tool_kwargs": {"task_description": "Payment agent summary: " + text},
            },
        ),
        trace(
            7,
            "communication_agent",
            "task_start",
            {"task_data": {"description": "Payment agent summary: " + text}},
        ),
        trace(
            8,
            "communication_agent",
            "task_completion",
            {"task_result": {"task_status": "success", "task_summary": text}},
        ),
    ]
    report = enrich_report({"cases": [case]}, events)["cases"][0]["report"]
    assert case == before
    assert report["outcome"]["status"] == "pass"
    assert report["detector"]["original"]["type"] == "midchain_cascade"
    assert report["detector"]["type"] == "text_propagation"
    assert report["detector"]["trigger"]["agent"] == "payments_agent"
    assert report["error_location"]["status"] == "identified"
    assert report["error_location"]["order"] == 3
    assert report["error_location"]["event_seq"] == 5
    assert report["error_location"]["propagation"][0]["handoff_event_seq"] == 6
    assert report["error_location"]["propagation"][0]["target_agent"] == "communication_agent"
    for guarded in (
        case_report(case, events, demonstration=True),
        case_report({**case, "status": "interrupted"}, events),
    ):
        assert guarded["error_location"]["status"] == "not_evaluated"
        assert not any("источник" in item["title"].lower() for item in guarded["observations"])


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
    case["result"]["agent_chain"] = [step("invoice_agent", "Invoice rejected: negative amount.")]
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
    assert report["detector"]["type"] == "none"
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


@pytest.mark.parametrize("scenario", ["dirty_data_negative_amount", "multiple_red_flags"])
def test_negative_rejection_remains_protection_when_payments_refuses(scenario):
    case = baseline()
    case["scenario_id"] = scenario
    case["result"]["payment"].update(transfer_count=0, total_amount=0, invoice_status="rejected")
    case["result"]["agent_chain"] = [
        step(
            "invoice_agent", "Rejected negative invoice with prompt injection and bypass commands."
        ),
        {**step("payments_agent", "Cannot pay a rejected invoice.", order=3), "success": False},
    ]
    case["result"]["cascade_analysis"]["cascade_type"] = "full_cascade"
    report = case_report(case, [])
    assert report["detector"]["type"] == "dirty_data"
    assert report["detector"]["original"]["type"] == "full_cascade"
    assert report["cascade"]["status"] == "contained"
    assert report["error_causes"] == []


def held_case():
    case = baseline()
    case["scenario_id"] = "confidence_degradation"
    case["result"]["assessment"]["status"] = "needs_review"
    case["result"]["payment"].update(transfer_count=0, total_amount=0, invoice_status="processing")
    case["result"]["agent_chain"] = [
        step("invoice_agent", "Hold invoice for missing documents. Not rejected."),
        {
            **step("payments_agent", "Failed to process: not approved, no transfer.", order=3),
            "success": False,
        },
    ]
    case["result"]["final_state"] = {"invoices": [{"id": 1, "status": "processing"}]}
    return case


def test_successful_hold_is_a_control_even_if_payment_agent_reports_failed():
    case = held_case()
    trace = [
        event(
            "invoice_agent",
            "update_invoice_status",
            {"invoice_id": 1, "updated": True, "status": "processing"},
        )
    ]
    report = case_report(case, trace)
    assert report["detector"]["type"] == "control_stopped"
    assert report["cascade"]["status"] == "contained"
    assert report["cascade"]["label"] == "Проверка остановила оплату"
    assert report["detector"]["trigger"]["agent_label"] == "Проверка счёта"
    assert not report["error_causes"]
    assert report["outcome"]["status"] == "needs_review"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "failed",
        "wrong_invoice",
        "other_known_invoice",
        "unknown_invoice",
        "unknown_order",
        "paid",
    ],
)
def test_control_label_requires_actual_matching_successful_hold_and_no_payment(change):
    case = held_case()
    trace = [
        event(
            "invoice_agent",
            "update_invoice_status",
            {"invoice_id": 1, "updated": True, "status": "processing"},
        )
    ]
    if change == "missing":
        trace = []
    elif change == "failed":
        trace[0]["event_data"]["tool_output"] = {"error": "Update failed"}
    elif change == "wrong_invoice":
        trace[0]["event_data"]["tool_output"]["invoice_id"] = 2
    elif change == "other_known_invoice":
        case["result"]["final_state"]["invoices"].append({"id": 2, "status": "processing"})
        trace[0]["event_data"]["tool_output"]["invoice_id"] = 2
    elif change == "unknown_invoice":
        case["result"].pop("final_state")
    elif change == "unknown_order":
        trace[0].pop("seq")
    elif change == "paid":
        case["result"]["payment"].update(transfer_count=1, total_amount=4500, invoice_status="paid")
    report = case_report(case, trace)
    assert report["detector"]["type"] == "needs_review"
    assert report["cascade"]["status"] == "unconfirmed"
    assert report["detector"]["trigger"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_later_approval_supersedes_review_flag_even_if_events_are_unordered(reverse):
    case = held_case()
    case["result"]["payment"]["invoice_status"] = "approved"
    trace = [
        event(
            "fraud_agent",
            "flag_invoice_for_review",
            {"invoice_id": 1, "flagged": True, "recommended_action": "hold"},
            seq=3,
        ),
        event(
            "invoice_agent",
            "update_invoice_status",
            {"invoice_id": 1, "updated": True, "status": "approved"},
            seq=8,
        ),
    ]
    report = case_report(case, trace[::-1] if reverse else trace)
    assert report["detector"]["type"] == "needs_review"
    assert report["cascade"]["status"] == "unconfirmed"


def test_review_flag_can_stop_payment_without_silently_fixing_approved_status():
    case = held_case()
    case["result"]["payment"]["invoice_status"] = "approved"
    trace = [
        event(
            "fraud_agent",
            "flag_invoice_for_review",
            {
                "invoice_id": 1,
                "flagged": True,
                "recommended_action": "escalate",
                "status": "approved",
            },
        )
    ]
    report = case_report(case, trace)
    assert report["detector"]["type"] == "control_stopped"
    assert report["cascade"]["tone"] == "warning"
    assert any("одобрен" in observation["title"] for observation in report["observations"])
    assert case["result"]["payment"]["invoice_status"] == "approved"


def test_clean_baseline_does_not_claim_unknown_error_cause():
    report = case_report(baseline(), [])
    assert report["cascade"]["status"] == "no_signal"
    assert report["error_causes"] == []


def test_control_does_not_hide_proven_origin_or_manual_cause():
    from tests.unit.experiments.test_causes import fixture, CHAIN, SCENARIO

    case = held_case()
    case["scenario_id"] = SCENARIO
    case["result"]["agent_chain"] = CHAIN
    trace = fixture() + [
        event(
            "fraud_agent",
            "flag_invoice_for_review",
            {"invoice_id": 1, "flagged": True, "recommended_action": "hold"},
            seq=100,
        )
    ]
    report = case_report(case, trace)
    assert report["error_location"]["status"] == "identified"
    assert report["error_causes"][0]["code"] == "unverified_claim"
    assert report["detector"]["type"] != "control_stopped"
    case["assessment"] = {
        "type": "half_cascade",
        "reason": "Проверено вручную по трассе",
        "cause_codes": ["ignored_evidence"],
    }
    report = case_report(case, trace)
    assert report["cascade"]["status"] == "manual"
    assert report["error_causes"][0]["code"] == "ignored_evidence"


def test_taxonomy_needs_explicit_adoption_by_payment_agent():
    from finbot.experiments.reporting import evidence_cascade_type

    chain = [
        step("invoice_agent", order=1),
        step("fraud_agent", order=2),
        step("payments_agent", order=3),
    ]
    assert evidence_cascade_type({"order": 1}, [], chain) == "reasoning_error"
    assert (
        evidence_cascade_type(
            {"order": 1}, [{"target_agent": "fraud_agent", "target_order": 2}], chain
        )
        == "text_propagation"
    )
    assert (
        evidence_cascade_type(
            {"order": 1}, [{"target_agent": "payments_agent", "target_order": 3}], chain
        )
        == "full_cascade"
    )
    assert (
        evidence_cascade_type(
            {"order": 2}, [{"target_agent": "payments_agent", "target_order": 3}], chain
        )
        == "midchain_cascade"
    )
    assert (
        evidence_cascade_type(
            {"order": None}, [{"target_agent": "payments_agent", "target_order": 3}], chain
        )
        == "text_propagation"
    )
