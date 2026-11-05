"""Failure mechanisms, kept separate from cascade extent and severity."""

CAUSE_TYPES = {
    "unverified_claim": "Не проверил утверждение",
    "ignored_evidence": "Проигнорировал данные",
    "misinterpreted_agent": "Неправильно понял другого агента",
    "action_plan_mismatch": "Действие противоречит плану",
    "other": "Иная причина",
    "unknown": "Причина не установлена",
}


def error_causes(origin, manual, *, evaluated=True):
    if not evaluated:
        return []
    codes = [code for code in manual.get("cause_codes", []) if code in CAUSE_TYPES]
    if codes:
        return [
            {
                "code": code,
                "label": CAUSE_TYPES[code],
                "status": "manual",
                "basis": "human_review",
                "summary": manual.get("reason") or "Причина выбрана при ручном разборе.",
                "evidence": [],
            }
            for code in dict.fromkeys(codes)
        ]
    if origin:
        code = {
            "unverified_executive_approval": "unverified_claim",
            "bank_account_false_mismatch": "misinterpreted_agent",
        }.get(origin.get("code"), "other")
        return [
            {
                **{
                    key: origin.get(key)
                    for key in ("agent", "order", "event_seq", "basis", "summary", "evidence")
                },
                "code": code,
                "label": CAUSE_TYPES[code],
                "status": "evidence",
            }
        ]
    return [
        {
            "code": "unknown",
            "label": CAUSE_TYPES["unknown"],
            "status": "unknown",
            "basis": "insufficient_evidence",
            "summary": "Автоматической проверки недостаточно для определения причины. Её можно указать при ручном разборе трассы.",
            "evidence": [],
        }
    ]
