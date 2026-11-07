"""Connection/schema checks and an optional write probe that always rolls back."""

from contextlib import closing
from uuid import uuid4

from psycopg2.extras import Json

TABLES = (
    "cm_runs",
    "cm_cases",
    "cm_agent_runs",
    "cm_llm_calls",
    "cm_events",
    "cm_series_configs",
    "cm_series",
    "cm_series_runs",
    "cm_case_observations",
    "cm_agent_iterations",
    "cm_iteration_evaluations",
)


def check_archive(archive, *, write_probe=False):
    with closing(archive.connect()) as conn:
        conn.set_session(readonly=not write_probe)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_database(), current_user, (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())"
                )
                database, user, tls = cur.fetchone()
                cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=ANY(%s)",
                    (list(TABLES),),
                )
                present = {r[0] for r in cur.fetchall()}
                missing = sorted(set(TABLES) - present)
                cur.execute(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN ('cm_series','cm_case_observations')"
                )
                columns = set(cur.fetchall())
                missing_columns = [
                    f"{table}.{column}"
                    for table, column in (
                        ("cm_series", "source_updated_at"),
                        ("cm_case_observations", "observations"),
                    )
                    if (table, column) not in columns
                ]
                result = {
                    "database": database,
                    "user": user,
                    "tls": tls,
                    "missing_tables": missing,
                    "missing_columns": missing_columns,
                }
                if missing or missing_columns:
                    result["ok"] = False
                    return result
                if write_probe:
                    ids = [str(uuid4()) for _ in range(6)]
                    series_config, series, run, case, agent, iteration = ids
                    cur.execute(
                        "INSERT INTO public.cm_series_configs(id,name,created_at,manifest) VALUES (%s,'Storage probe',now(),%s)",
                        (series_config, Json({"probe": True})),
                    )
                    cur.execute(
                        "INSERT INTO public.cm_series(id,name,created_at,status,manifest,plan,metadata) VALUES (%s,'Storage probe',now(),'completed',%s,%s,%s)",
                        (series, Json({"probe": True}), Json([]), Json({})),
                    )
                    cur.execute(
                        "INSERT INTO public.cm_runs(id,source_kind,source_run_id,status,is_demo,config,metrics,metadata) VALUES (%s,'storage_probe',%s,'completed',true,%s,%s,%s)",
                        (run, run, Json({"probe": True}), Json({}), Json({})),
                    )
                    cur.execute(
                        "INSERT INTO public.cm_series_runs(run_id,series_id) VALUES (%s,%s)",
                        (run, series),
                    )
                    cur.execute(
                        """INSERT INTO public.cm_cases(id,run_id,source_case_id,scenario_id,repeat_index,execution_status,report,result,metrics,human_review,scenario_snapshot,cascade_location,provenance)
                        VALUES (%s,%s,%s,'storage_probe',1,'completed',%s,%s,%s,%s,%s,%s,%s)""",
                        (case, run, case, *[Json({}) for _ in range(7)]),
                    )
                    cur.execute(
                        "INSERT INTO public.cm_case_observations(case_id,observations) VALUES (%s,%s)",
                        (case, Json({"probe": True})),
                    )
                    cur.execute(
                        """INSERT INTO public.cm_agent_runs(id,case_id,source_agent_run_id,agent_name,result,input,metrics,provenance)
                        VALUES (%s,%s,%s,'storage_probe',%s,%s,%s,%s)""",
                        (agent, case, agent, *[Json({}) for _ in range(4)]),
                    )
                    cur.execute(
                        """INSERT INTO public.cm_agent_iterations(id,case_id,agent_run_id,source_agent_run_id,agent_name,iteration_index,execution_status,vanilla_confidence,lexical_confidence,trace)
                        VALUES (%s,%s,%s,%s,'storage_probe',1,'completed',0.8,%s,%s)""",
                        (iteration, case, agent, agent, Json({"value": 1}), Json({})),
                    )
                    cur.execute(
                        "INSERT INTO public.cm_iteration_evaluations(iteration_id,status,confidence,assessment) VALUES (%s,'completed',0.7,%s)",
                        (iteration, Json({"method": "storage_probe"})),
                    )
                    cur.execute(
                        """SELECT count(*) FROM public.cm_runs r JOIN public.cm_cases c ON c.run_id=r.id
                        JOIN public.cm_agent_iterations i ON i.case_id=c.id
                        JOIN public.cm_iteration_evaluations e ON e.iteration_id=i.id
                        WHERE r.id=%s AND e.confidence=0.7 AND i.vanilla_confidence=0.8""",
                        (run,),
                    )
                    if cur.fetchone()[0] != 1:
                        raise ValueError("Archive write probe failed")
                    result["write_probe"] = {"verified": True, "rolled_back": True}
                result["ok"] = True
                return result
        finally:
            conn.rollback()
