"""Independent annotation validation and append-only archive reviews."""

import hashlib
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

from finbot.experiments.research_outcomes import review_targets


def reviewer_key(user_id):
    return hashlib.sha256(("cascademap-reviewer:" + str(user_id)).encode()).hexdigest()[:24]


def validate_review(case, data, events=()):
    targets = {t["agent_ref"]: t for t in review_targets(case, events)}
    iterations = (case.get("observations") or {}).get("iterations", [])
    for item in [*(data.get("agent_errors") or []), *(data.get("claims") or [])]:
        ref = item["agent_ref"]
        source = item.get("source_agent_ref")
        if ref not in targets or source and source not in targets:
            raise ValueError("Указанное исполнение агента отсутствует в этой попытке")
        if source:
            a, b = targets[source].get("order"), targets[ref].get("order")
            if source == ref or a is not None and b is not None and a >= b:
                raise ValueError("Источник должен предшествовать получателю")
        index = item.get("iteration_index")
        if index is not None and not any(
            r["source_agent_run_id"] == ref and r["iteration_index"] == index for r in iterations
        ):
            raise ValueError("Итерация не найдена у указанного исполнения")
        evidence = set(case.get("evidence_sequences") or []) | {
            int(e["seq"]) for e in events if str(e.get("seq", "")).isdigit()
        }
        if set(item.get("evidence_seq") or []) - evidence:
            raise ValueError("Событие-доказательство отсутствует в этой попытке")
    payment = (case.get("result") or {}).get("payment") or {}
    outcome = data.get("outcome") or {}
    if payment.get("transfer_count") == 0 and (
        outcome.get("correct_payment") is True or outcome.get("harm_type") == "wrong_payment"
    ):
        raise ValueError("В сохранённой попытке нет переводов; подтвердить перевод нельзя")
    if (
        outcome.get("correct_payment") is True
        and payment.get("transfer_count") is not None
        and payment["transfer_count"] != 1
    ):
        raise ValueError("Корректный перевод должен быть выполнен ровно один раз")
    return data


def archive_case(archive, case_id):
    from finbot.experiments.analytics_data import normalize, serializable
    from finbot.experiments.archive_projection import sanitize

    with closing(archive.connect()) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.cm_cases WHERE id=%s", (case_id,))
            case = cur.fetchone()
            if case is None:
                raise KeyError("Сценарий не найден")
            cur.execute("SELECT * FROM public.cm_runs WHERE id=%s", (case["run_id"],))
            run = cur.fetchone()
            tables = {"cm_cases": [case], "cm_runs": [run]}
            for table in (
                "cm_agent_runs",
                "cm_agent_iterations",
                "cm_case_observations",
                "cm_case_reviews",
                "cm_events",
            ):
                cur.execute(f"SELECT * FROM public.{table} WHERE case_id=%s", (case_id,))
                tables[table] = cur.fetchall()
            cur.execute(
                "SELECT * FROM public.cm_iteration_evaluations WHERE iteration_id IN (SELECT id FROM public.cm_agent_iterations WHERE case_id=%s)",
                (case_id,),
            )
            tables["cm_iteration_evaluations"] = cur.fetchall()
    normalized = normalize(serializable(tables), "archive")["cases"][0]
    normalized["run"] = serializable(run)
    normalized["review_targets"] = review_targets(normalized)
    return sanitize(normalized)


def save_archive_review(archive, case_id, data, author, expected_revision=None):
    """Same run lock as result sync. No model/result fields are overwritten."""
    with (
        closing(archive.connect()) as conn,
        conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        cur.execute("SELECT run_id FROM public.cm_cases WHERE id=%s", (case_id,))
        parent = cur.fetchone()
        if not parent:
            raise KeyError("Сценарий не найден")
        lock = int.from_bytes(
            hashlib.sha256(str(parent["run_id"]).encode()).digest()[:8], "big", signed=True
        )
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
        cur.execute(
            "SELECT execution_status,human_review FROM public.cm_cases WHERE id=%s", (case_id,)
        )
        case = cur.fetchone()
        if not case or case["execution_status"] != "completed":
            raise ValueError("Оценка доступна для завершённого сценария")
        cur.execute(
            "SELECT assessment FROM public.cm_case_reviews WHERE case_id=%s ORDER BY reviewed_at NULLS FIRST,id",
            (case_id,),
        )
        previous = [r["assessment"] for r in cur.fetchall()]
        if expected_revision is not None and expected_revision != len(previous):
            raise ValueError("Разметка изменилась; обновите детали перед сохранением")
        latest = previous[-1] if previous else (case["human_review"] or {})
        base = latest if latest.get("reviewer_key") == author else {}
        review = {
            **base,
            **data,
            "review_id": str(uuid4()),
            "reviewer_key": author,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "schema_version": 1,
            "revision": len(previous) + 1,
        }
        cur.execute(
            "INSERT INTO public.cm_case_reviews(id,case_id,reviewer_key,reviewed_at,schema_version,assessment) VALUES (%s,%s,%s,%s,1,%s)",
            (review["review_id"], case_id, author, review["reviewed_at"], Json(review)),
        )
    return review
