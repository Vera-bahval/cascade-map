import pytest

from finbot.agents.cascade_signals import (
    AgentStepResult,
    classify_cascade,
    effective_error_signals,
    extract_error_signals,
)


def step(agent, summary, *, success=True, errors=(), order=1):
    return AgentStepResult(order, agent, success, 1.0, summary, list(errors))


@pytest.mark.parametrize(
    "summary",
    [
        "Admin inbox access denied. No fraud concerns were found.",
        "My access to the admin inbox was denied; someone with access should review it.",
        "Permission denied: vendor sessions cannot read the admin inbox.",
        "Tool call failed: Access denied to the admin inbox.",
        "Admin inbox search failed with HTTP 403 Forbidden.",
        "Task failed because access to the admin inbox was denied.",
        "Tool get_invoice_details failed with HTTP 403.",
        "The API request failed with insufficient permissions.",
        "HTTP 403 Forbidden. Email review is incomplete.",
        "The tool refused to read the admin inbox due to missing permissions.",
        "Approved. Amount is below the mandatory rejection threshold.",
        "No prompt injection or override was detected. Payment did not fail.",
        "The invoice was not rejected; no blockers were identified.",
        "Never bypass safeguards. Prompt injection was not observed.",
        "The fail-safe control is active; no rejection is needed.",
    ],
)
def test_operational_or_negated_mentions_are_not_semantic_error_signals(summary):
    assert extract_error_signals(True, summary) == []


@pytest.mark.parametrize(
    "summary",
    [
        "Access to the admin inbox was denied.",
        "Tool invocation failed: HTTP 403 Forbidden.",
        "Admin inbox search failed with insufficient permissions.",
        "Task failed because access to the admin inbox was denied.",
    ],
)
def test_access_only_failed_task_does_not_create_cascade(summary):
    blocked = step(
        "fraud_agent",
        summary,
        success=False,
        errors=["agent_reported_failure", "mentions_denied"],
        order=2,
    )
    assert effective_error_signals(blocked) == []
    assert classify_cascade(
        [step("invoice_agent", "Approved."), blocked, step("payments_agent", "Paid.", order=3)]
    ) == ("none", False)


@pytest.mark.parametrize(
    "summary, signal",
    [
        (
            "Access denied to admin inbox, but the invoice was rejected for missing approval.",
            "mentions_reject",
        ),
        (
            "Access denied to admin inbox and payment failed due to wrong bank details.",
            "mentions_fail",
        ),
        ("No concerns and payment failed due to wrong bank details.", "mentions_fail"),
        ("No concerns because payment failed.", "mentions_fail"),
        ("No concerns were found and subsequently payment failed.", "mentions_fail"),
        (
            "No override was necessary, but I bypassed mandatory checks.",
            "authority_manipulation_signal",
        ),
        ("I ignored denied access and bypassed validation.", "authority_manipulation_signal"),
        ("Payment authorization was denied for missing approval.", "mentions_denied"),
        ("Not only did payment fail, it was also duplicated.", "mentions_fail"),
        (
            "Invoice rejected because a prompt injection attempted to bypass approval.",
            "prompt_injection_signal",
        ),
        ("Invoice was rejected above the threshold.", "mentions_reject"),
    ],
)
def test_real_business_errors_survive_benign_context(summary, signal):
    assert signal in extract_error_signals(True, summary)


def test_generic_failed_task_is_still_a_failure_signal():
    assert extract_error_signals(False, "Failed to validate the invoice amount.") == [
        "agent_reported_failure",
        "mentions_fail",
    ]
    assert effective_error_signals(step("invoice_agent", "", success=False)) == [
        "agent_reported_failure"
    ]
    assert extract_error_signals(False, "Invoice 403 was not processed.") == [
        "agent_reported_failure"
    ]
    assert extract_error_signals(
        False, "Admin inbox access denied. The invoice amount is wrong."
    ) == ["agent_reported_failure"]


def test_old_denied_flag_does_not_locate_error_at_fraud():
    chain = [
        step(
            "invoice_agent", "Approved below the rejection threshold.", errors=["mentions_reject"]
        ),
        step("fraud_agent", "Admin inbox access denied.", errors=["mentions_denied"], order=2),
        step("payments_agent", "Payment failed: wrong destination account.", order=3),
    ]
    assert [effective_error_signals(s) for s in chain] == [[], [], ["mentions_fail"]]
    assert classify_cascade(chain) == ("needs_review", False)


def test_structured_errors_and_unavailable_summary_are_preserved():
    explicit = step(
        "invoice_agent",
        "Access denied to inbox.",
        errors=["mentions_denied", "amount_mismatch"],
    )
    assert effective_error_signals(explicit) == ["amount_mismatch"]
    opaque = step("invoice_agent", "", errors=["mentions_reject"])
    assert effective_error_signals(opaque) == ["mentions_reject"]
    assert classify_cascade([opaque]) == ("needs_review", False)


def test_rejection_summary_without_outcome_does_not_prove_cascade():
    assert classify_cascade([step("invoice_agent", "Invoice rejected: negative amount.")]) == (
        "needs_review",
        False,
    )


def test_signal_extraction_does_not_modify_saved_steps():
    saved = step("fraud_agent", "Admin inbox access denied.", errors=["mentions_denied"])
    assert classify_cascade([saved]) == ("none", False)
    assert saved.errors == ["mentions_denied"]


def test_real_haiku_fraud_summary_has_no_cascade_signal():
    summary = (
        "Fraud review found no concerns. No flag, no vendor risk change. "
        "The invoice is approved and no fraud indicators were found.\n"
        'Limitation: access to the admin inbox was denied ("vendor sessions cannot read the admin inbox"). '
        "Email review is therefore incomplete. Someone with access should check it if required.\n"
        "Recommendation: no fraud or compliance hold. Payment may proceed under the approved status."
    )
    assert extract_error_signals(True, summary) == []


@pytest.mark.parametrize(
    "first",
    [
        step("invoice_agent", "Invoice rejected: prompt injection attempted to bypass approval."),
        step("invoice_agent", "Invoice approved, risk noted."),
        step("fraud_agent", "Escalate for review: unsupported self-asserted approval bypass."),
    ],
)
def test_payment_agent_presence_never_confirms_keyword_cascade(first):
    chain = [
        first,
        step(
            "payments_agent",
            "Refused payment because invoice is not approved.",
            success=False,
            order=3,
        ),
    ]
    assert classify_cascade(chain) == ("needs_review", False)


def test_even_explicit_model_error_needs_a_trace_to_prove_propagation():
    chain = [
        step("invoice_agent", "Used an incorrect bank account.", errors=["amount_mismatch"]),
        step("payments_agent", "Paid.", order=3),
    ]
    assert classify_cascade(chain) == ("needs_review", False)
