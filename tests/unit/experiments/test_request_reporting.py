import json

import pytest

from finbot.experiments.request_reporting import request_summary
from finbot.experiments.schemas import AssessmentInput


def test_public_request_summary_keeps_billing_without_prompts():
    event = {
        "seq": 42,
        "at": "2026-11-03T10:00:00Z",
        "agent_name": "invoice_agent",
        "record": {
            "success": True,
            "duration_ms": 10,
            "request": {"prompt": "private-prompt"},
            "response": {"model": "actual-model", "content": "private-answer"},
            "usage": {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 0},
        },
    }
    summary = request_summary(
        event, {"model": "requested", "input_price": 2, "output_price": 5, "currency": "USD"}
    )
    assert summary["cost"] == pytest.approx(0.0025)
    assert summary["model"] == "actual-model"
    assert summary["event_seq"] == 42
    assert summary["at"] == event["at"]
    assert "private" not in json.dumps(summary)
    assert request_summary(event, {})["cost"] is None


@pytest.mark.parametrize(
    "codes",
    [["not_a_cause"], ["unverified_claim", "unverified_claim"], ["unknown", "ignored_evidence"]],
)
def test_review_rejects_invalid_causes(codes):
    with pytest.raises(ValueError):
        AssessmentInput(type="full_cascade", reason="Evidence from the trace", cause_codes=codes)


def test_older_review_client_can_omit_causes():
    review = AssessmentInput(type="half_cascade", reason="Evidence from the trace")
    assert "cause_codes" not in review.model_dump(exclude_unset=True)
