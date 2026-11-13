from copy import deepcopy

from finbot.experiments.analytics import AnalyticsFilter, summarize
from tests.unit.experiments.test_analytics import dataset


def annotated_dataset():
    data = dataset()
    template = data["cases"][0]
    data["cases"] = []
    for i, (source_error, target_error) in enumerate(
        [(True, True), (True, False), (False, True), (False, False)]
    ):
        case = deepcopy(template)
        case["id"] = str(i)
        case["agents"] = [
            {"id": "a", "source_agent_run_id": "a", "agent_name": "invoice_agent", "step_order": 1},
            {"id": "b", "source_agent_run_id": "b", "agent_name": "fraud_agent", "step_order": 2},
        ]
        case["human_review"]["agent_errors"] = [
            {
                "agent_ref": "a",
                "input_state": "clean",
                "error_present": source_error,
                "error_origin": "own" if source_error else "none",
            },
            {
                "agent_ref": "b",
                "source_agent_ref": "a",
                "input_state": "tainted" if source_error else "clean",
                "error_present": target_error,
                "error_origin": (
                    "adopted"
                    if source_error and target_error
                    else "own" if target_error else "none"
                ),
            },
        ]
        data["cases"].append(case)
    return data


def test_egr_and_observational_adoption_require_clean_or_tainted_human_labels():
    data = annotated_dataset()
    research = summarize(data, AnalyticsFilter())["research"]
    fraud = next(a for a in research["errors"]["by_agent"] if a["agent"] == "fraud_agent")
    assert fraud["egr"]["value"] == fraud["ear_observational"]["value"] == 0.5
    assert fraud["egr"]["n"] == 2 and not fraud["interventional"]
    dependency = research["errors"]["dependencies"][0]
    assert dependency["rr_observational"] == 1
    assert dependency["p_error_given_error"]["n"] == 2
    for case in data["cases"]:
        for row in case["human_review"]["agent_errors"]:
            row["input_state"] = "unknown"
    research = summarize(data, AnalyticsFilter())["research"]
    assert all(row["egr"]["value"] is None for row in research["errors"]["by_agent"])


def test_zero_baseline_rr_is_unknown_not_infinite():
    data = annotated_dataset()
    for case in data["cases"]:
        label = case["human_review"]["agent_errors"][1]
        if label["input_state"] == "clean":
            label.update(error_present=False, error_origin="none")
    dependency = summarize(data, AnalyticsFilter())["research"]["errors"]["dependencies"][0]
    assert dependency["p_error_given_correct"]["value"] == 0
    assert dependency["rr_observational"] is None


def test_unknown_labels_produce_bounds_instead_of_negative_examples():
    data = dataset()
    data["cases"] = data["cases"][:2]
    data["cases"][1]["human_review"] = {}
    result = summarize(data, AnalyticsFilter())["research"]["outcomes"]
    assert result["cascade_bounds"] == {"n": 2, "unresolved": 1, "lower": 0.5, "upper": 1}


def test_repeat_agreement_and_seed_spread_do_not_mix_other_parameters():
    data = dataset()
    template = data["cases"][0]
    data["cases"] = []
    second = deepcopy(data["runs"][0])
    second.update(id="second", config={"temperature": 0.7, "seed": 2})
    data["runs"] = [data["runs"][0], second]
    for run_id, classes in [
        ("run-a", ["full_cascade", "no_cascade"]),
        ("second", ["full_cascade", "full_cascade"]),
    ]:
        for index, kind in enumerate(classes):
            case = deepcopy(template)
            case.update(id=f"{run_id}-{index}", run_id=run_id, human_review={"type": kind})
            data["cases"].append(case)
    stability = summarize(data, AnalyticsFilter())["research"]["stability"]
    assert len(stability) == 1
    assert stability[0]["repeat_agreement"]["value"] == 0.5
    assert stability[0]["between_seed_frequency"]["std"] == 0.25
    data["runs"][1]["config"]["top_p"] = 0.9
    stability = summarize(data, AnalyticsFilter())["research"]["stability"]
    assert len(stability) == 2
    assert all(s["between_seed_frequency"]["n"] == 0 for s in stability)


def test_laundering_requires_same_claim_review_and_distinct_humans_for_kappa():
    data = dataset()
    data["cases"] = data["cases"][:1]
    case = data["cases"][0]
    case["human_review"]["claims"] = [
        {
            "agent_ref": "span",
            "claim_key": "CEO-approval",
            "incoming": "attributed",
            "outgoing": "asserted",
        }
    ]
    case["assessment_history"] = [
        {"reviewer_key": "one", "type": "full_cascade", "reviewed_at": "2026-11-01"},
        {"reviewer_key": "one", "type": "no_cascade", "reviewed_at": "2026-11-02"},
    ]
    outcome = summarize(data, AnalyticsFilter())["research"]["outcomes"]
    assert outcome["reviewed_claim_laundering"]["value"] == 1
    assert outcome["interrater_kappa"]["n"] == 0
    case["assessment_history"].append(
        {"reviewer_key": "two", "type": "full_cascade", "reviewed_at": "2026-11-03"}
    )
    assert summarize(data, AnalyticsFilter())["research"]["outcomes"]["interrater_kappa"]["n"] == 1
