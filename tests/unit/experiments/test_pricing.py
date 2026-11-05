import pytest

from finbot.experiments.pricing import calls_cost, request_cost, sum_costs


def usage(**updates):
    return {"input_tokens": 1000, "output_tokens": 500, "cached_input_tokens": 200, **updates}


def prices(**updates):
    return {
        "input_price": 1,
        "output_price": 2,
        "cached_input_price": 0.5,
        "currency": "RUB",
        **updates,
    }


def test_actual_usage_and_cache_tariff_are_counted_once():
    result = request_cost(usage(reasoning_tokens=100), prices(price_source="Team tariff"))
    assert result["cost"] == 0.0019
    assert result["currency"] == "RUB"
    assert result["cost_basis"] == "tariff_estimate"
    assert result["pricing"]["price_source"] == "Team tariff"


@pytest.mark.parametrize(
    "counts,tariffs,basis",
    [
        (usage(), {}, "missing_tariff"),
        (usage(input_tokens=None, estimated_input_tokens=1000), prices(), "missing_usage"),
        (usage(output_tokens=None), prices(), "missing_usage"),
        (usage(cached_input_tokens=1001), prices(), "invalid_usage"),
        (usage(cached_input_tokens=-1), prices(), "invalid_usage"),
        (usage(cached_input_tokens="invalid"), prices(), "invalid_usage"),
        (usage(input_tokens=-1), prices(), "missing_usage"),
        (usage(input_tokens=True), prices(), "missing_usage"),
        (usage(input_tokens=1.5), prices(), "missing_usage"),
        (usage(), prices(input_price=float("nan")), "missing_tariff"),
        (usage(), prices(output_price=-1), "missing_tariff"),
        (usage(usage_missing_calls=1), prices(), "missing_usage"),
    ],
)
def test_unknown_or_invalid_billing_is_not_zero(counts, tariffs, basis):
    result = request_cost(counts, tariffs)
    assert result["cost"] is None
    assert result["cost_basis"] == basis


def test_zero_cache_requires_no_cached_tariff_and_keeps_small_costs():
    result = request_cost(
        usage(input_tokens=1, output_tokens=1, cached_input_tokens=0),
        prices(input_price=0.0001, output_price=0.0001, cached_input_price=None),
    )
    assert result["cost"] == 0.0000000002


def test_explicit_free_tariff_is_distinct_from_unknown():
    result = request_cost(usage(), prices(input_price=0, output_price=0, cached_input_price=0))
    assert result["cost"] == 0
    assert result["cost_basis"] == "tariff_estimate"


@pytest.mark.parametrize(
    "counts,tariffs",
    [
        (usage(cached_input_tokens=None), prices()),
        (usage(), prices(cached_input_price=None)),
        (usage(cached_usage_missing_calls=1), prices()),
    ],
)
def test_unknown_cache_uses_normal_input_tariff_with_an_explicit_estimate_label(counts, tariffs):
    result = request_cost(counts, tariffs)
    assert result["cost"] == 0.002
    assert result["cost_basis"] == "tariff_estimate_no_cache_discount"


def test_equal_cache_tariff_does_not_need_cache_usage():
    result = request_cost(usage(cached_input_tokens=None), prices(cached_input_price=1))
    assert result["cost"] == 0.002
    assert result["cost_basis"] == "tariff_estimate"


def test_mixed_cache_coverage_sums_each_request_instead_of_partial_cached_tokens():
    result = calls_cost([{"usage": usage()}, {"usage": usage(cached_input_tokens=None)}], prices())
    assert result["cost"] == 0.0039
    assert result["cost_basis"] == "tariff_estimate_no_cache_discount"


def test_unknown_request_or_missing_trace_makes_the_total_unknown():
    assert calls_cost([{"usage": usage()}, {}], prices())["cost"] is None
    partial = calls_cost([{"usage": usage()}], prices(), complete=False)
    assert partial["cost"] is None
    assert partial["cost_basis"] == "missing_usage"


def test_totals_do_not_mix_currencies_or_unknown_costs():
    known = request_cost(usage(), prices())
    mixed = sum_costs([known, {**known, "currency": "USD"}], prices())
    assert mixed["cost"] is None
    assert mixed["cost_basis"] == "invalid_usage"
    unknown = sum_costs([known, {"cost": None, "cost_basis": "missing_usage"}], prices())
    assert unknown["cost"] is None


def test_no_requests_cost_zero_only_with_known_tariffs_and_complete_coverage():
    assert calls_cost([], prices())["cost"] == 0
    assert calls_cost([], {})["cost"] is None
    assert calls_cost([], prices(), complete=False)["cost"] is None
