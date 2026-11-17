"""Configurable intervals and pilot planning at fixed experimental conditions."""

import pytest
from pydantic import ValidationError

from finbot.experiments.analytics import AnalyticsFilter, fraction, summarize
from finbot.experiments.analytics_statistics import (
    apply_confidence_level,
    required_observations,
    sample_plan,
    wilson_interval,
)
from test_analytics import dataset


def test_selected_interval_preserves_legacy_and_interval_opt_out():
    rate = fraction(51, 391)
    original = rate["wilson95"][:]
    disabled = fraction(1, 3, interval=False)
    apply_confidence_level([rate, disabled], 0.8)
    assert rate["wilson95"] == original
    assert rate["confidence_interval"][0] > original[0]
    assert rate["confidence_interval"][1] < original[1]
    assert disabled["confidence_interval"] is None


@pytest.mark.parametrize("p", [0, 0.03, 0.5, 1])
@pytest.mark.parametrize("level", [0.8, 0.95, 0.999])
def test_planned_sample_size_is_minimum_for_wilson_width(p, level):
    n = required_observations(p, level, 0.05)
    bounds = wilson_interval(p * n, n, level)
    assert (bounds[1] - bounds[0]) / 2 <= 0.05
    bounds = wilson_interval(p * (n - 1), n - 1, level)
    assert (bounds[1] - bounds[0]) / 2 > 0.05


def test_more_confidence_or_precision_requires_more_observations():
    assert required_observations(0.5, 0.8, 0.05) == 163
    assert required_observations(0.5, 0.95, 0.05) == 381
    assert required_observations(0.5, 0.8, 0.025) > 163


def test_zero_events_does_not_imply_zero_required_repetitions():
    plan = sample_plan(fraction(0, 5), 10, 0.8, 0.05)
    assert 5 < plan["required_known_pilot"] <= plan["required_known_conservative"]
    assert plan["additional_completed"] == plan["additional_known"] * 2
    assert not plan["precision_reached"]


def test_no_labels_and_reached_precision():
    empty = sample_plan(fraction(0, 0), 10, 0.8, 0.05)
    assert empty["additional_completed"] is None
    assert empty["required_known_pilot"] is None
    assert empty["status"] == "no_known_outcomes"
    reached = sample_plan(fraction(51, 391), 674, 0.8, 0.05)
    assert reached["precision_reached"]
    assert reached["additional_known"] == reached["additional_completed"] == 0


@pytest.mark.parametrize(
    "values",
    [
        {"confidence_level": 1},
        {"confidence_level": 0.49},
        {"margin_of_error": 0},
        {"margin_of_error": float("nan")},
        {"planning_metric": "unknown"},
    ],
)
def test_invalid_planning_input(values):
    with pytest.raises(ValidationError):
        AnalyticsFilter(**values)


def test_summary_plans_use_exact_conditions_and_requested_metric():
    filters = AnalyticsFilter(confidence_level=0.9, planning_metric="p_casc")
    report = summarize(dataset(), filters)
    plan = report["sample_planning"]
    assert plan["confidence_level"] == 0.9
    assert len(plan["groups"]) == len(report["groups"])
    for group, planned in zip(report["groups"], plan["groups"], strict=True):
        assert group["configuration_signature"] == planned["configuration_signature"]
        assert planned["observed"] == group["p_casc"]
        assert planned["observed"]["confidence_level"] == 0.9
