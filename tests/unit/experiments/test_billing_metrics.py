from copy import deepcopy

import pytest

from finbot.experiments.archive_projection import build_archive
from finbot.experiments.executor import metrics_from_calls, summarize_job
from finbot.experiments.reporting import enrich_report
from finbot.experiments.request_reporting import billing_metrics
from finbot.experiments.store import Store
from tests.unit.experiments.test_executor import MemoryStore


def fixture():
    config = {"model": "gemma", "input_price": 1, "output_price": 2, "cached_input_price": 0.5}
    job = {
        "id": "priced-run",
        "status": "completed",
        "config": config,
        "connection": {},
        "metrics": {"cost": 999},
        "cases": [
            {"id": "a", "status": "completed", "metrics": {"llm_calls": 2}},
            {"id": "b", "status": "completed", "metrics": {"llm_calls": 1}},
        ],
    }
    events = []
    for seq, case_id, cached in [(1, "a", 200), (2, "a", None), (3, "b", 200)]:
        events.append(
            {
                "seq": seq,
                "case_id": case_id,
                "event_type": "llm_call",
                "record": {
                    "metadata": {"interaction_id": f"request-{seq}"},
                    "success": True,
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cached_input_tokens": cached,
                    },
                },
            }
        )
    return job, events


def test_saved_run_detail_and_archive_have_matching_per_request_scenario_and_run_estimates():
    job, events = fixture()
    before = deepcopy(job)
    detail = enrich_report(job, events)
    archived = build_archive(job, events)
    assert job == before
    assert [case["metrics"]["cost"] for case in detail["cases"]] == [0.0039, 0.0019]
    assert detail["metrics"]["cost"] == 0.0058
    assert detail["metrics"]["cost_basis"] == "tariff_estimate_no_cache_discount"
    assert detail["cases"][0]["requests"][1]["cost"] == 0.002
    assert archived["run"]["metrics"]["cost"] == detail["metrics"]["cost"]
    assert [case["metrics"]["cost"] for case in archived["cases"]] == [0.0039, 0.0019]
    assert sum(call["cost"] for call in archived["calls"]) == pytest.approx(0.0058)


@pytest.mark.parametrize("missing", ["usage", "event", "pending_request", "unknown_start_id"])
def test_unknown_request_makes_case_and_whole_run_unknown_but_preserves_other_case(missing):
    job, events = fixture()
    if missing == "usage":
        events[0]["record"].pop("usage")
    elif missing == "event":
        events.pop(0)
    else:
        events.append(
            {
                "case_id": "a",
                "event_type": "llm_request_start",
                "event_data": (
                    {"interaction_id": "unreturned"} if missing == "pending_request" else {}
                ),
            }
        )
    for result in (enrich_report(job, events), build_archive(job, events)):
        assert result["cases"][0]["metrics"]["cost"] is None
        assert result["cases"][0]["metrics"]["cost_basis"] == "missing_usage"
        assert result["cases"][1]["metrics"]["cost"] == 0.0019
        assert result.get("run", result)["metrics"]["cost"] is None


def test_absent_historical_tariff_is_never_replaced_by_a_stored_old_total():
    job, events = fixture()
    job["config"] = {"model": "gemma"}
    detail = enrich_report(job, events)
    assert detail["metrics"]["cost"] is None
    assert detail["metrics"]["cost_basis"] == "missing_tariff"


def test_future_case_and_run_metrics_sum_the_same_request_estimates():
    job, events = fixture()
    for case in job["cases"]:
        calls = [event["record"] for event in events if event["case_id"] == case["id"]]
        case["metrics"] = metrics_from_calls(calls, 1, job["config"])
    store = MemoryStore(job)
    totals = summarize_job(store, job)
    assert totals["cost"] == 0.0058
    assert totals["cost_basis"] == "tariff_estimate_no_cache_discount"
    assert totals["currency"] == "USD"
    assert store.job["metrics"]["cost"] == totals["cost"]


def test_zero_rates_are_visible_as_zero_at_case_and_run_level():
    job, events = fixture()
    job["config"].update(input_price=0, output_price=0, cached_input_price=0)
    detail = enrich_report(job, events)
    assert detail["metrics"]["cost"] == 0
    assert all(case["metrics"]["cost"] == 0 for case in detail["cases"])
    assert detail["metrics"]["cost_basis"] == "tariff_estimate"


@pytest.mark.parametrize("response", [[{"unexpected": "response"}], "invalid provider body"])
def test_malformed_provider_response_does_not_break_report_or_billing(response):
    job, events = fixture()
    events[0]["record"].update(response=response, success=False)
    events[0]["record"].pop("usage")
    detail = enrich_report(job, events)
    request = detail["cases"][0]["requests"][0]
    assert request["model"] == "gemma"
    assert request["success"] is False
    assert request["cost"] is None
    assert request["cost_basis"] == "missing_usage"
    assert detail["metrics"]["cost"] is None


def test_history_projection_reads_only_billing_fields_and_keeps_snapshots_unchanged(tmp_path):
    job, events = fixture()
    store = Store(tmp_path / "billing.db")
    connection = store.save_connection("a", {"name": "mock", "kind": "mock", "base_url": ""})
    saved = store.create_job(
        "a", "experiment", {**job["config"], "connection_id": connection["id"]}, job["cases"]
    )
    for event in events:
        event["record"]["request"] = {"messages": ["private-prompt-not-needed-for-history"]}
        event["record"]["response"] = {"content": "private-response-not-needed-for-history"}
        store.append_event(saved["id"], event)
    before = store.get_job(saved["id"])
    thin = store.billing_events(saved["id"])
    assert len(thin) == 3
    assert "private-prompt" not in str(thin)
    assert "private-response" not in str(thin)
    assert all(set(event["record"]) == {"usage", "metadata"} for event in thin)
    assert billing_metrics(before, thin)["run"] == billing_metrics(before, events)["run"]
    assert billing_metrics(before, thin)["run"]["cost"] == 0.0058
    assert store.get_job(saved["id"]) == before
