import copy

import pytest

from finbot.agents.cascade_signals import _infer_confidence
from finbot.experiments.agent_spans import begin_iteration, run_in_span, span_fields
from finbot.experiments.evaluation import confidence_profiles
from finbot.experiments.observations import iteration_trace, lexical_confidence


def test_vanilla_is_unchanged_and_negation_aware_proxy_is_separate():
    assert _infer_confidence(True, "No risk or concern was found.") == 0.8
    assert lexical_confidence(True, "No risk or concern was found.")["value"] == 1
    assert (
        lexical_confidence(True, "No risk was found, but suspicious activity requires review.")[
            "value"
        ]
        == 0.8
    )
    assert lexical_confidence(True, "Risk was not detected.")["value"] == 1
    assert lexical_confidence(False, "No risk.")["value"] == 0.3


def test_final_completion_and_repeated_invocations_do_not_merge():
    events = []
    for span, summary in [("first", "No risk."), ("second", "Suspicious invoice.")]:
        for kind, data in [
            ("iteration_start", {}),
            (
                "llm_call",
                {
                    "record": {
                        "success": True,
                        "observation": {
                            "tool_calls": [
                                {
                                    "name": "complete_task",
                                    "arguments": {
                                        "task_status": "success",
                                        "task_summary": summary,
                                    },
                                }
                            ]
                        },
                    }
                },
            ),
            ("agent_span_end", {"event_data": {"status": "completed"}}),
        ]:
            events.append(
                {
                    "agent_run_id": span,
                    "agent_name": "invoice_agent",
                    "iteration_index": 1,
                    "event_type": kind,
                    "at": "2026-11-06T00:00:00+00:00",
                    **data,
                }
            )
    before = copy.deepcopy(events)
    rows = iteration_trace(events)
    assert len(rows) == 2
    assert all(r["status"] == "completed" for r in rows)
    assert rows[0]["lexical_confidence"]["value"] == 1
    assert rows[0]["vanilla_confidence"] == 0.9
    assert events == before


def test_tool_only_iteration_has_no_invented_vanilla_confidence():
    row = iteration_trace(
        [
            {
                "agent_run_id": "agent",
                "agent_name": "invoice_agent",
                "iteration_index": 1,
                "event_type": "llm_call",
                "record": {
                    "success": True,
                    "observation": {"content": "", "tool_calls": [{"name": "get_invoice_details"}]},
                },
            }
        ]
    )[0]
    assert row["status"] == "incomplete"
    assert row["vanilla_confidence"] is None
    assert row["judge"]["confidence"] is None


def test_confidence_degradation_uses_endpoints_within_each_invocation_and_keeps_sign():
    rows = [
        {
            "source_agent_run_id": span,
            "agent_name": "invoice_agent",
            "iteration_index": i,
            "judge": {"status": "completed", "confidence": value},
        }
        for span, values in [("one", [0.5, 0.8]), ("two", [0.9, 0.2])]
        for i, value in enumerate(values, 1)
    ]
    profiles = confidence_profiles(rows)
    assert [p["confidence_degradation"] for p in profiles] == [-0.3, 0.7]
    rows[1]["judge"]["confidence"] = None
    assert confidence_profiles(rows)[0]["confidence_degradation"] is None


@pytest.mark.asyncio
async def test_nested_agents_have_their_own_iteration_context():
    async def capture(**event):
        pass

    async def child():
        assert "iteration_index" not in span_fields()
        begin_iteration(1)
        assert span_fields()["iteration_index"] == 1
        return {}

    async def parent():
        begin_iteration(3)
        await run_in_span(capture, "invoice_agent", {}, child)
        assert span_fields()["iteration_index"] == 3
        return {}

    await run_in_span(capture, "orchestrator_agent", {}, parent)
    assert span_fields() == {}
