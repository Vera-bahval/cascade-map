import copy

from finbot.experiments.archive_projection import _location
from finbot.experiments.archive_reanalysis import prepare_reanalysis


def report():
    return {
        "version": 2,
        "cascade": {"status": "text_propagation"},
        "detector": {
            "version": "evidence-v2",
            "type": "text_propagation",
            "trigger": {"agent": "payments_agent", "order": 3},
            "explanation": "False mismatch propagated after payment",
            "limitation": "Only observed trace evidence",
        },
        "error_location": {
            "status": "identified",
            "agent": "payments_agent",
            "order": 3,
            "event_seq": 42,
            "basis": "tool_output_contradiction",
            "evidence": [{"event_seq": 40, "account_match": True}],
            "propagation": [
                {"source_agent": "payments_agent", "target_agent": "communication_agent"}
            ],
        },
    }


def test_location_uses_current_detector_not_old_summary_flags():
    case = {
        "result": {
            "agent_chain": [{"agent": "fraud_agent", "order": 2, "errors": ["mentions_denied"]}]
        }
    }
    location = _location(case, report())
    assert location["first_signal_agent"] == "payments_agent"
    assert location["first_signal_step"] == 3
    assert location["detected_origin_agent"] == "payments_agent"
    assert location["detected_origin_event_seq"] == 42
    assert location["confirmed_origin_agent"] is None
    assert location["detected_origin_evidence"] == [{"event_seq": 40, "account_match": True}]


def test_unknown_evidence_does_not_create_origin_or_resurrect_raw_signal():
    current = report()
    current["detector"]["trigger"] = None
    current["error_location"]["status"] = "unknown"
    location = _location(
        {"result": {"agent_chain": [{"agent": "fraud_agent", "errors": ["mentions_denied"]}]}},
        current,
    )
    assert location["first_signal_agent"] is None
    assert location["detected_origin_agent"] is None
    assert location["detected_origin_evidence"] == []
    assert location["confirmed_origin_agent"] is None


def test_manual_origin_is_preserved_separately_from_detector_origin():
    current = report()
    current["cascade"]["status"] = "manual"
    location = _location(
        {"assessment": {"origin_agent": "invoice_agent", "origin_step": 1}}, current
    )
    assert location["detected_origin_agent"] == "payments_agent"
    assert location["confirmed_origin_agent"] == "invoice_agent"
    assert location["confirmed_origin_step"] == 1


def test_prepare_reanalysis_preserves_original_evidence_and_review(monkeypatch):
    row = {
        "source_case_id": "source-case",
        "scenario_id": "baseline_clean",
        "execution_status": "completed",
        "result": {"cascade_analysis": {"cascade_type": "midchain_cascade"}},
        "human_review": {"type": "no_cascade", "reason": "Human assessment"},
        "scenario_snapshot": {"id": "baseline_clean"},
        "metrics": {"input_tokens": 123},
        "provenance": {"call_coverage_complete": True, "original_source": "legacy"},
    }
    original = copy.deepcopy(row)
    events = [{"event_type": "task_completion", "seq": 42}]
    captured = {}

    def analyze(case, supplied_events):
        captured.update(case=case, events=supplied_events)
        return report()

    monkeypatch.setattr("finbot.experiments.archive_reanalysis.case_report", analyze)
    result = prepare_reanalysis(row, events)
    assert row == original
    assert captured["case"]["assessment"] == row["human_review"]
    assert captured["case"]["result"] == row["result"]
    assert captured["events"] == events
    assert set(result) == {"report", "cascade_location", "provenance"}
    assert result["provenance"]["call_coverage_complete"] is True
    assert result["provenance"]["report_version"] == 2
    assert result["provenance"]["detector_version"] == "evidence-v2"
