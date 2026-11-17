"""Dashboard denominators and terminal failures stay independent of missing labels."""

from copy import deepcopy

from finbot.experiments.analytics import AnalyticsFilter, summarize
from test_analytics import dataset


def test_dashboard_includes_failed_attempts_without_assigning_unknown_harm():
    report = summarize(dataset(), AnalyticsFilter(confidence_level=0.8))
    dashboard = report["dashboard"]
    assert all(row["model"] != "B" for row in dashboard["runs"])
    trigger = next(row for row in dashboard["runs"] if row["scenario"] == "trigger")
    assert trigger["attempts"] == 3
    assert trigger["completed"] == 2
    assert trigger["metrics"]["technical"]["positive"] == 1
    assert trigger["metrics"]["technical"]["n"] == 3
    assert trigger["metrics"]["harm"]["n"] == 0
    assert trigger["metrics"]["harm"]["confidence_interval"] is None
    assert trigger["metrics"]["cascade"]["n"] == 2
    assert trigger["metrics"]["technical"]["confidence_level"] == 0.8


def test_dashboard_retains_run_with_only_failure_and_separates_parameters():
    data = dataset()
    run = deepcopy(data["runs"][0])
    run["id"] = "failed-only"
    run["config"]["top_p"] = 0.6
    data["runs"].append(run)
    case = deepcopy(data["cases"][0])
    case.update(id="failure", run_id="failed-only", execution_status="failed")
    data["cases"].append(case)
    report = summarize(data, AnalyticsFilter())
    row = next(r for r in report["dashboard"]["runs"] if r["run_id"] == "failed-only")
    assert row["completed"] == 0
    assert row["metrics"]["technical"]["value"] == 1
    assert row["metrics"]["cascade"]["n"] == 0
    cells = [r for r in report["dashboard"]["cells"] if r["scenario"] == "trigger"]
    assert len(cells) == 2
    assert sum(r["attempts"] for r in cells) == 4
