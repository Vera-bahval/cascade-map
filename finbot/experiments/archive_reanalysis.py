"""Recompute archived analysis from stored evidence without rerunning agents."""

import hashlib
import json
from collections import defaultdict
from contextlib import closing

from psycopg2.extras import Json, RealDictCursor

from finbot.experiments.archive_projection import _location
from finbot.experiments.reporting import REPORT_EVENTS, case_report


def prepare_reanalysis(row, events):
    case = {
        "id": row["source_case_id"],
        "scenario_id": row["scenario_id"],
        "status": row["execution_status"],
        "result": row["result"],
        "metrics": row["metrics"],
        "assessment": row["human_review"],
        "scenario_snapshot": row["scenario_snapshot"],
    }
    report = case_report(case, events)
    return {
        "report": report,
        "cascade_location": _location(case, report),
        "provenance": {
            **row["provenance"],
            "report_version": report.get("version"),
            "detector_version": (report.get("detector") or {}).get("version"),
        },
    }


def reanalyze(archive, *, dry_run=False, case_ids=None):
    selected = [str(value) for value in case_ids or []]
    selection = " AND c.id = ANY(%s::uuid[])" if selected else ""
    totals = {"runs": 0, "cases": 0, "changed": 0, "dry_run": dry_run}
    with closing(archive.connect()) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT r.id FROM public.cm_runs r "
                "JOIN public.cm_cases c ON c.run_id=r.id "
                "WHERE NOT r.is_demo" + selection + " ORDER BY r.id",
                (selected,) if selected else (),
            )
            run_ids = [row[0] for row in cur.fetchall()]
    for run_id in run_ids:
        with closing(archive.connect()) as conn:
            if dry_run:
                conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                if not dry_run:
                    lock = int.from_bytes(
                        hashlib.sha256(str(run_id).encode()).digest()[:8], "big", signed=True
                    )
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
                cur.execute(
                    "SELECT id FROM public.cm_runs WHERE id=%s AND NOT is_demo"
                    + ("" if dry_run else " FOR UPDATE"),
                    (str(run_id),),
                )
                if cur.fetchone() is None:
                    continue
                cur.execute(
                    "SELECT c.* FROM public.cm_cases c WHERE c.run_id=%s"
                    + selection
                    + " ORDER BY c.id"
                    + ("" if dry_run else " FOR UPDATE"),
                    (str(run_id), selected) if selected else (str(run_id),),
                )
                rows = list(cur.fetchall())
                if not rows:
                    continue
                cur.execute(
                    "SELECT case_id, CASE WHEN event_type='llm_call' "
                    "THEN payload #- '{record,request}' ELSE payload END AS payload "
                    "FROM public.cm_events WHERE case_id = ANY(%s::uuid[]) "
                    "AND event_type = ANY(%s) "
                    "ORDER BY case_id, CASE WHEN source_seq ~ '^[0-9]+$' "
                    "THEN source_seq::numeric END NULLS LAST, occurred_at NULLS LAST, source_seq",
                    ([str(row["id"]) for row in rows], list(REPORT_EVENTS)),
                )
                grouped = defaultdict(list)
                for event in cur.fetchall():
                    grouped[str(event["case_id"])].append(event["payload"])
                for row in rows:
                    analysis = prepare_reanalysis(row, grouped[str(row["id"])])
                    if any(row[key] != value for key, value in analysis.items()):
                        if not dry_run:
                            cur.execute(
                                "UPDATE public.cm_cases SET report=%s, cascade_location=%s, "
                                "provenance=%s WHERE id=%s",
                                tuple(
                                    Json(
                                        value,
                                        dumps=lambda x: json.dumps(
                                            x, ensure_ascii=False, allow_nan=False
                                        ),
                                    )
                                    for value in (
                                        analysis[key]
                                        for key in ("report", "cascade_location", "provenance")
                                    )
                                )
                                + (str(row["id"]),),
                            )
                        totals["changed"] += 1
                    totals["cases"] += 1
                totals["runs"] += 1
    return totals
