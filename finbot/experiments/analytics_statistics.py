"""Descriptive binomial intervals and planning for fixed experimental conditions."""

from math import ceil, sqrt
from statistics import NormalDist


def wilson_interval(positive, total, confidence_level=0.95):
    if not total:
        return None
    p = positive / total
    z = NormalDist().inv_cdf((1 + confidence_level) / 2)
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0, center - margin), min(1, center + margin)]


def apply_confidence_level(value, confidence_level):
    """Add selected intervals while preserving the public wilson95 field."""
    if isinstance(value, list):
        for item in value:
            apply_confidence_level(item, confidence_level)
    elif isinstance(value, dict):
        if {"positive", "n", "value", "wilson95"} <= value.keys():
            value["confidence_level"] = confidence_level
            value["confidence_interval"] = (
                wilson_interval(value["positive"], value["n"], confidence_level)
                if value["wilson95"] is not None
                else None
            )
        else:
            for item in value.values():
                apply_confidence_level(item, confidence_level)


def required_observations(p, confidence_level, margin_of_error):
    """Minimum planned n with Wilson full interval width <= 2 * margin."""
    z = NormalDist().inv_cdf((1 + confidence_level) / 2)
    low, high = 1, max(1, ceil(z * z / (4 * margin_of_error**2)))
    while low < high:
        middle = (low + high) // 2
        interval = wilson_interval(p * middle, middle, confidence_level)
        if (interval[1] - interval[0]) / 2 <= margin_of_error:
            high = middle
        else:
            low = middle + 1
    return low


def sample_plan(rate, completed, confidence_level, margin_of_error):
    """Pilot plan uses the highest variance within the pilot Wilson interval."""
    n = rate["n"]
    interval = wilson_interval(rate["positive"], n, confidence_level)
    conservative = required_observations(0.5, confidence_level, margin_of_error)
    result = {
        "observed": rate,
        "completed": completed,
        "coverage": n / completed if completed else None,
        "current_half_width": (interval[1] - interval[0]) / 2 if interval else None,
        "required_known_conservative": conservative,
        "planning_proportion": None,
        "required_known_pilot": None,
        "additional_known": None,
        "additional_completed": None,
        "precision_reached": False,
        "status": "no_known_outcomes",
    }
    if not n:
        return result
    planning_p = min(max(0.5, interval[0]), interval[1])
    required = required_observations(planning_p, confidence_level, margin_of_error)
    reached = result["current_half_width"] <= margin_of_error
    additional = 0 if reached else max(0, required - n)
    result.update(
        planning_proportion=planning_p,
        required_known_pilot=required,
        additional_known=additional,
        additional_completed=ceil(additional * completed / n),
        precision_reached=reached,
        status="precision_reached" if reached else "more_observations_needed",
    )
    return result
