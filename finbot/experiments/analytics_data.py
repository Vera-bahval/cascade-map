"""Analytics readers share a normalized projection, never execution credentials."""

import json
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from finbot.experiments.analytics import selected_runs
from finbot.experiments.archive import TABLES, PostgresArchive
from finbot.experiments.archive_health import TABLES as ARCHIVE_TABLES
from finbot.experiments.archive_projection import build_archive, sanitize


def serializable(value):
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def normalize(tables, source):
    runs = tables.get("cm_runs", [])
    links = {r["run_id"]: r["series_id"] for r in tables.get("cm_series_runs", [])}
    for run in runs:
        run["series_id"] = links.get(run["id"]) or run.get("metadata", {}).get("series_id")
    agents, iterations, observations, tool_events = {}, {}, {}, {}
    for row in tables.get("cm_events", []):
        if row["event_type"] == "tool_call_start":
            tool_events.setdefault(row["case_id"], []).append(row)
    for row in tables.get("cm_case_observations", []):
        observations[row["case_id"]] = dict(row.get("observations") or {})
    evaluations = {r["iteration_id"]: r for r in tables.get("cm_iteration_evaluations", [])}
    for row in tables.get("cm_agent_iterations", []):
        evaluation = evaluations.get(row["id"], {})
        judge = {
            **(evaluation.get("assessment") or {}),
            **{
                k: evaluation[k]
                for k in ("status", "confidence", "error_present")
                if k in evaluation
            },
        }
        iterations.setdefault(row["case_id"], []).append(
            {**row, "status": row["execution_status"], "judge": judge}
        )
    for row in tables.get("cm_agent_runs", []):
        agents.setdefault(row["case_id"], []).append(row)
    cases = []
    for row in tables.get("cm_cases", []):
        observation = observations.get(row["id"], {})
        if row["id"] in iterations:
            observation["iterations"] = iterations[row["id"]]
        cases.append(
            {
                **row,
                "observations": observation,
                "agents": agents.get(row["id"], []),
                "tool_events": tool_events.get(row["id"], []),
            }
        )
    return {"source": source, "runs": runs, "cases": cases}


def local_tables(store, who):
    tables = {name: [] for name in ARCHIVE_TABLES}
    # One SQLite read transaction, no history's 200-run limit, no cross-owner reads.
    with store.db() as db:
        db.execute("BEGIN")
        jobs = [
            json.loads(r[0])
            for r in db.execute("SELECT data FROM jobs WHERE owner=? ORDER BY updated DESC", (who,))
        ]
        for job in jobs:
            if job.get("kind") != "experiment":
                continue
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM events WHERE job_id=? ORDER BY seq", (job["id"],)
                )
            ]
            bundle = build_archive(store._public_job(job), events)
            tables["cm_runs"].append(bundle["run"])
            for kind, table in TABLES.items():
                if kind != "run":
                    tables[table].extend(bundle.get(kind, []))
        tables["cm_series_configs"] = [
            json.loads(r[0])
            for r in db.execute("SELECT data FROM series_configs WHERE owner=?", (who,))
        ]
        tables["cm_series"] = [
            json.loads(r[0]) for r in db.execute("SELECT data FROM series WHERE owner=?", (who,))
        ]
    return tables


def inventory(tables):
    return [
        {"table": table, "rows": len(rows), "columns": sorted({key for row in rows for key in row})}
        for table, rows in tables.items()
    ]


def archive_read(
    filters, *, catalog=False, table=None, offset=0, limit=100, archive=None, all_rows=False
):
    archive = archive or PostgresArchive()
    with closing(archive.connect()) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT table_name,column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=ANY(%s) "
                "ORDER BY table_name,ordinal_position",
                (list(ARCHIVE_TABLES),),
            )
            columns = {}
            for row in cur.fetchall():
                columns.setdefault(row["table_name"], []).append(row["column_name"])
            required = {"cm_runs", "cm_cases", "cm_agent_runs", "cm_llm_calls", "cm_events"}
            if required - columns.keys():
                raise ValueError("В архиве отсутствуют базовые таблицы CascadeMap")
            tables = {name: [] for name in ARCHIVE_TABLES}
            cur.execute("SELECT * FROM public.cm_runs ORDER BY started_at DESC NULLS LAST,id")
            tables["cm_runs"] = serializable(cur.fetchall())
            if "cm_series_runs" in columns:
                cur.execute("SELECT * FROM public.cm_series_runs")
                tables["cm_series_runs"] = serializable(cur.fetchall())
            if catalog:
                counts = []
                for name in ARCHIVE_TABLES:
                    count = None
                    if name in columns:
                        cur.execute(
                            sql.SQL("SELECT count(*) AS n FROM public.{}").format(
                                sql.Identifier(name)
                            )
                        )
                        count = cur.fetchone()["n"]
                    counts.append(
                        {
                            "table": name,
                            "rows": count,
                            "columns": columns.get(name, []),
                            "available": name in columns,
                        }
                    )
                cur.execute("SELECT DISTINCT scenario_id FROM public.cm_cases ORDER BY scenario_id")
                scenarios = [r["scenario_id"] for r in cur.fetchall()]
                cur.execute(
                    "SELECT DISTINCT agent_name FROM public.cm_agent_runs ORDER BY agent_name"
                )
                agents = [r["agent_name"] for r in cur.fetchall()]
                return {
                    "runs": normalize(tables, "archive")["runs"],
                    "tables": counts,
                    "scenarios": scenarios,
                    "agents": agents,
                    "source": "archive",
                    "scope": "Общий PostgreSQL: данные, доступные настроенной учётной записи БД",
                }
            runs = selected_runs(normalize(tables, "archive")["runs"], filters)
            ids = [r["id"] for r in runs]
            tables["cm_runs"] = runs
            # All identifiers come from this fixed map; values remain bound parameters.
            scopes = {
                "cm_runs": ("t.id=ANY(%s::uuid[])", [ids]),
                "cm_cases": ("t.run_id=ANY(%s::uuid[])", [ids]),
                "cm_series_runs": ("t.run_id=ANY(%s::uuid[])", [ids]),
                "cm_series": (
                    "t.id IN (SELECT series_id FROM public.cm_series_runs WHERE run_id=ANY(%s::uuid[]))",
                    [ids],
                ),
                "cm_series_configs": ("true", []),
            }
            case_scope = "SELECT id FROM public.cm_cases WHERE run_id=ANY(%s::uuid[])"
            case_params = [ids]
            if filters.scenario_ids:
                case_scope += " AND scenario_id=ANY(%s)"
                case_params.append(filters.scenario_ids)
            scopes["cm_cases"] = ("t.id IN (" + case_scope + ")", case_params)
            for name in (
                "cm_agent_runs",
                "cm_llm_calls",
                "cm_events",
                "cm_case_observations",
                "cm_agent_iterations",
            ):
                scopes[name] = ("t.case_id IN (" + case_scope + ")", case_params)
            scopes["cm_iteration_evaluations"] = (
                "t.iteration_id IN (SELECT id FROM public.cm_agent_iterations WHERE case_id IN ("
                + case_scope
                + "))",
                case_params,
            )
            if table:
                if table not in columns:
                    return {"rows": [], "total": 0, "available": False}
                where, params = ("true", []) if all_rows else scopes[table]
                base = sql.SQL(" FROM public.{} t WHERE " + where).format(sql.Identifier(table))
                cur.execute(sql.SQL("SELECT count(*) AS n") + base, params)
                total = cur.fetchone()["n"]
                order = (
                    "id"
                    if "id" in columns[table]
                    else (
                        "iteration_id"
                        if table == "cm_iteration_evaluations"
                        else "case_id" if table == "cm_case_observations" else "run_id"
                    )
                )
                cur.execute(
                    sql.SQL("SELECT t.*")
                    + base
                    + sql.SQL(" ORDER BY {} LIMIT %s OFFSET %s").format(sql.Identifier(order)),
                    [*params, limit, offset],
                )
                return sanitize(
                    serializable({"rows": cur.fetchall(), "total": total, "available": True})
                )
            for name in (
                "cm_cases",
                "cm_agent_runs",
                "cm_case_observations",
                "cm_agent_iterations",
                "cm_iteration_evaluations",
            ):
                if name not in columns:
                    continue
                where, params = scopes[name]
                selection = "t.*"
                if name == "cm_cases":
                    selection = """t.id,t.run_id,t.scenario_id,t.execution_status,t.duration_ms,
                        t.metrics,t.human_review,t.cascade_location,
                        jsonb_build_object('payment',t.result->'payment',
                            'cascade_analysis',t.result->'cascade_analysis') AS result,
                        jsonb_build_object('detector',jsonb_build_object(
                            'type',t.report->'detector'->'type',
                            'original',t.report->'detector'->'original'),
                            'outcome',jsonb_build_object('status',t.report->'outcome'->'status')) AS report"""
                elif name == "cm_agent_runs":
                    selection = """t.id,t.case_id,t.source_agent_run_id,t.agent_name,t.step_order,
                        t.started_at,t.summary,t.input,t.heuristic_confidence,t.model_confidence,
                        t.metrics,t.provenance"""
                elif name == "cm_case_observations":
                    selection = """t.case_id,jsonb_build_object('iterations',
                        COALESCE(t.observations->'iterations','[]'::jsonb)) AS observations"""
                if name == "cm_agent_iterations":
                    selection = "t.id,t.case_id,t.source_agent_run_id,t.agent_name,t.iteration_index,t.execution_status,t.vanilla_confidence,t.lexical_confidence"
                cur.execute(
                    sql.SQL("SELECT " + selection + " FROM public.{} t WHERE " + where).format(
                        sql.Identifier(name)
                    ),
                    params,
                )
                tables[name] = serializable(cur.fetchall())
            where, params = scopes["cm_events"]
            cur.execute(
                sql.SQL(
                    "SELECT t.* FROM public.cm_events t WHERE "
                    + where
                    + " AND t.event_type='tool_call_start'"
                ).format(),
                params,
            )
            tables["cm_events"] = serializable(cur.fetchall())
            return normalize(tables, "archive")


def read_catalog(store, who, filters):
    if filters.source == "archive":
        return archive_read(filters, catalog=True)
    tables = local_tables(store, who)
    return {
        "runs": normalize(tables, "local")["runs"],
        "tables": inventory(tables),
        "scenarios": sorted({r["scenario_id"] for r in tables["cm_cases"]}),
        "agents": sorted({r["agent_name"] for r in tables["cm_agent_runs"]}),
        "source": "local",
        "scope": "Локальное хранилище: только текущее рабочее пространство; таблицы показаны в формате архивной проекции",
    }


def read_dataset(store, who, filters):
    if filters.source == "archive":
        return archive_read(filters)
    return normalize(local_tables(store, who), "local")


def read_rows(store, who, filters, table, offset, limit, all_rows=False):
    if filters.source == "archive":
        return archive_read(filters, table=table, offset=offset, limit=limit, all_rows=all_rows)
    tables = local_tables(store, who)
    data = normalize(tables, "local")
    run_ids = {r["id"] for r in selected_runs(data["runs"], filters)}
    case_ids = {
        c["id"]
        for c in data["cases"]
        if c["run_id"] in run_ids
        and (not filters.scenario_ids or c["scenario_id"] in filters.scenario_ids)
    }
    iteration_ids = {r["id"] for r in tables["cm_agent_iterations"] if r["case_id"] in case_ids}
    series_ids = {r["series_id"] for r in data["runs"] if r["id"] in run_ids and r.get("series_id")}

    def selected(row):
        if all_rows:
            return True
        if table == "cm_runs":
            return row["id"] in run_ids
        if table == "cm_cases":
            return row["id"] in case_ids
        if table == "cm_iteration_evaluations":
            return row["iteration_id"] in iteration_ids
        if table == "cm_series":
            return row["id"] in series_ids
        if table == "cm_series_configs":
            return True
        if table == "cm_series_runs":
            return row["run_id"] in run_ids
        return row.get("case_id") in case_ids

    rows = [r for r in tables[table] if selected(r)]
    return {"rows": rows[offset : offset + limit], "total": len(rows), "available": True}
