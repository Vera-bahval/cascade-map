"""Tariff estimates from measured usage; these are not provider invoices."""

from decimal import Decimal, InvalidOperation

PRICE_FIELDS = (
    "input_price",
    "output_price",
    "cached_input_price",
    "price_source",
    "price_checked_at",
)


def _number(value, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or integer and result != result.to_integral_value():
            return None
        return result
    except (InvalidOperation, ValueError):
        return None


def request_cost(usage, config):
    result = {
        "cost": None,
        "currency": config.get("currency", "USD"),
        "cost_basis": "missing_tariff",
        "pricing": {key: config[key] for key in PRICE_FIELDS if key in config},
    }
    input_price, output_price = (_number(config.get(k)) for k in ("input_price", "output_price"))
    if input_price is None or output_price is None:
        return result
    incoming, outgoing = (_number(usage.get(k), True) for k in ("input_tokens", "output_tokens"))
    if incoming is None or outgoing is None or usage.get("usage_missing_calls"):
        result["cost_basis"] = "missing_usage"
        return result
    cached_value = usage.get("cached_input_tokens")
    cached = _number(cached_value, True)
    if cached_value is not None and cached is None or cached is not None and cached > incoming:
        result["cost_basis"] = "invalid_usage"
        return result
    cache_price = _number(config.get("cached_input_price"))
    cache_unknown = cached is None or usage.get("cached_usage_missing_calls")
    no_discount = (cache_unknown or cached and cache_price is None) and cache_price != input_price
    if cache_unknown or cache_price is None:
        cached = Decimal(0)
    total = (
        (incoming - cached) * input_price + cached * (cache_price or 0) + outgoing * output_price
    ) / 1_000_000
    result.update(
        cost=float(total.quantize(Decimal("0.000000000001"))),
        cost_basis="tariff_estimate_no_cache_discount" if no_discount else "tariff_estimate",
    )
    return result


def sum_costs(entries, config, *, complete=True):
    """Sum per-request estimates; never present a partial trace as the full cost."""
    result = request_cost({"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}, config)
    if result["cost"] is None:
        return result
    if not complete:
        return {**result, "cost": None, "cost_basis": "missing_usage"}
    entries = list(entries)
    for entry in entries:
        if entry.get("cost") is None:
            return {
                **result,
                "cost": None,
                "cost_basis": entry.get("cost_basis") or "missing_usage",
            }
        if _number(entry["cost"]) is None or entry.get("currency") != result["currency"]:
            return {**result, "cost": None, "cost_basis": "invalid_usage"}
    total = sum((Decimal(str(entry["cost"])) for entry in entries), Decimal(0))
    result["cost"] = float(total.quantize(Decimal("0.000000000001")))
    if any(entry.get("cost_basis") == "tariff_estimate_no_cache_discount" for entry in entries):
        result["cost_basis"] = "tariff_estimate_no_cache_discount"
    return result


def calls_cost(calls, config, *, complete=True):
    return sum_costs(
        [request_cost(call.get("usage") or {}, config) for call in calls],
        config,
        complete=complete,
    )
