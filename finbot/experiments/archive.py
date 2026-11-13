"""Shared result archive; execution and credentials remain in the local store."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

JSON_COLUMNS = {
    "run": {"config", "metrics", "metadata"},
    "cases": {
        "report",
        "result",
        "metrics",
        "human_review",
        "scenario_snapshot",
        "cascade_location",
        "provenance",
    },
    "agents": {"result", "input", "metrics", "provenance"},
    "calls": {"effective_params", "request", "response", "error", "provenance"},
    "events": {"payload"},
    "iterations": {"trace", "lexical_confidence"},
    "evaluations": {"assessment"},
    "case_observations": {"observations"},
    "series_runs": set(),
    "reviews": {"assessment"},
}
TABLES = {
    "run": "cm_runs",
    "cases": "cm_cases",
    "agents": "cm_agent_runs",
    "calls": "cm_llm_calls",
    "events": "cm_events",
    "iterations": "cm_agent_iterations",
    "evaluations": "cm_iteration_evaluations",
    "case_observations": "cm_case_observations",
    "series_runs": "cm_series_runs",
    "reviews": "cm_case_reviews",
}


def configured():
    from finbot.config import settings

    return bool(settings.FINBOT_RESULTS_PG_HOST)


def error_message(exc):
    code = getattr(exc, "pgcode", None)
    if isinstance(exc, psycopg2.OperationalError):
        return "PostgreSQL недоступен. Проверьте адрес, доступы и TLS-сертификат. Отправка будет повторена."
    if isinstance(exc, FileNotFoundError):
        return (
            "Не найден TLS-сертификат PostgreSQL. Проверьте путь и подключение файла к контейнеру."
        )
    return f"Результат пока не отправлен в PostgreSQL: {type(exc).__name__}" + (
        f" ({code})" if code else ""
    )


def timestamp(value):
    if not value:
        return None
    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return date if date.tzinfo else date.replace(tzinfo=UTC)


class PostgresArchive:
    def __init__(self, parameters=None):
        if parameters is None:
            from finbot.config import settings

            cert = Path(settings.FINBOT_RESULTS_PG_SSLROOTCERT).expanduser()
            if not settings.FINBOT_RESULTS_PG_SSLROOTCERT or not cert.is_file():
                raise FileNotFoundError("PostgreSQL TLS certificate")
            parameters = {
                "host": settings.FINBOT_RESULTS_PG_HOST,
                "port": settings.FINBOT_RESULTS_PG_PORT,
                "dbname": settings.FINBOT_RESULTS_PG_DATABASE,
                "user": settings.FINBOT_RESULTS_PG_USER,
                "password": settings.FINBOT_RESULTS_PG_PASSWORD,
                "sslmode": "verify-full",
                "sslrootcert": str(cert.resolve()),
            }
        self.parameters = dict(parameters)

    def connect(self):
        return psycopg2.connect(
            **self.parameters,
            connect_timeout=5,
            application_name="cascademap-results",
            options="-c statement_timeout=30000 -c lock_timeout=5000",
        )

    def initialize(self):
        directory = Path(__file__).with_name("sql")
        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(70644312)")
            cur.execute(
                "SELECT c.relname, pg_get_userbyid(c.relowner)=current_user FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=ANY(%s)",
                (["cm_runs", "cm_cases", "cm_agent_runs", "cm_llm_calls", "cm_events"],),
            )
            existing = cur.fetchall()
            if not existing or all(row[1] for row in existing):
                cur.execute((directory / "results_v1.sql").read_text())
            else:
                # A shared writer may have DML/REFERENCES without owning the base
                # schema. Do not ALTER another developer's tables or views.
                if len(existing) != 5:
                    raise ValueError("Base archive migration must be applied by its owner")
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='cm_llm_calls' AND column_name=ANY(%s)",
                    (["cost", "currency", "cost_basis"],),
                )
                if len(cur.fetchall()) != 3:
                    raise ValueError("Base billing migration must be applied by its owner")
            cur.execute((directory / "results_v2.sql").read_text())
            cur.execute((directory / "results_v3.sql").read_text())

    def _insert(self, cur, kind, rows):
        if not rows:
            return
        columns = list(rows[0])
        if any(set(row) != set(columns) for row in rows):
            raise ValueError("Archive rows have inconsistent columns")
        statement = sql.SQL("INSERT INTO public.{} ({}) VALUES %s").format(
            sql.Identifier(TABLES[kind]), sql.SQL(",").join(map(sql.Identifier, columns))
        )
        values = []
        for row in rows:
            values.append(
                tuple(
                    (
                        Json(
                            row[col],
                            dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False),
                        )
                        if col in JSON_COLUMNS[kind]
                        else row[col]
                    )
                    for col in columns
                )
            )
        execute_values(cur, statement.as_string(cur), values, page_size=25)

    def save(self, bundle):
        if bundle.get("series"):
            self.save_series(bundle["series"])
        run = dict(bundle["run"])
        run["source_updated_at"] = timestamp(run["metadata"].get("source_updated_at"))
        lock = int.from_bytes(hashlib.sha256(run["id"].encode()).digest()[:8], "big", signed=True)
        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            cur.execute(
                """SELECT source_updated_at,
                    (SELECT count(*) FROM public.cm_cases c WHERE c.run_id=r.id),
                    (SELECT count(*) FROM public.cm_llm_calls l JOIN public.cm_cases c ON c.id=l.case_id WHERE c.run_id=r.id)
                    FROM public.cm_runs r WHERE id=%s FOR UPDATE""",
                (run["id"],),
            )
            previous = cur.fetchone()
            if (
                previous
                and previous[0]
                and (run["source_updated_at"] is None or previous[0] > run["source_updated_at"])
            ):
                return {"status": "stale", "id": run["id"]}
            if previous and (
                len(bundle["cases"]) < previous[1]
                or (run["source_updated_at"] == previous[0] and len(bundle["calls"]) < previous[2])
            ):
                return {"status": "stale", "id": run["id"]}
            # Replace one immutable-source snapshot atomically, including all child rows.
            cur.execute(
                "SELECT v.* FROM public.cm_case_reviews v JOIN public.cm_cases c ON c.id=v.case_id WHERE c.run_id=%s",
                (run["id"],),
            )
            columns = [d.name for d in cur.description]
            reviews = {str(row[0]): dict(zip(columns, row)) for row in cur.fetchall()}
            reviews.update({str(row["id"]): row for row in bundle.get("reviews", [])})
            cur.execute("DELETE FROM public.cm_runs WHERE id=%s", (run["id"],))
            self._insert(cur, "run", [run])
            for kind in ("cases", "agents", "calls", "events"):
                self._insert(cur, kind, bundle[kind])
            for kind in ("series_runs", "case_observations", "iterations", "evaluations"):
                self._insert(cur, kind, bundle.get(kind, []))
            case_ids = {str(c["id"]) for c in bundle["cases"]}
            self._insert(
                cur, "reviews", [row for row in reviews.values() if str(row["case_id"]) in case_ids]
            )
        return {
            "status": "synced",
            "id": run["id"],
            **{k: len(bundle[k]) for k in ("cases", "agents", "calls", "events")},
        }

    def save_series_config(self, item):
        from finbot.experiments.archive_projection import sanitize

        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.cm_series_configs(id,name,created_at,manifest)
                VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (
                    item["id"],
                    item["manifest"]["name"],
                    timestamp(item["created_at"]),
                    Json(sanitize(item["manifest"])),
                ),
            )

    def save_series(self, item):
        from finbot.experiments.archive_projection import sanitize

        with closing(self.connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.cm_series
                (id,name,created_at,started_at,finished_at,status,manifest,plan,metadata,source_updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET started_at=EXCLUDED.started_at,
                finished_at=EXCLUDED.finished_at,status=EXCLUDED.status,plan=EXCLUDED.plan,
                source_updated_at=EXCLUDED.source_updated_at,archived_at=now()
                WHERE cm_series.source_updated_at IS NULL OR cm_series.source_updated_at<=EXCLUDED.source_updated_at""",
                (
                    item["id"],
                    item["name"],
                    timestamp(item["created_at"]),
                    timestamp(item.get("started_at")),
                    timestamp(item.get("finished_at")),
                    item["status"],
                    Json(sanitize(item["manifest"])),
                    Json(sanitize(item["runs"])),
                    Json(
                        {
                            "code_revision": item["code_revision"],
                            "scenario_hash": item["scenario_hash"],
                        }
                    ),
                    timestamp(
                        item.get("updated_at")
                        or item.get("finished_at")
                        or item.get("started_at")
                        or item["created_at"]
                    ),
                ),
            )


def sync_once(store, archive=None):
    if archive is None and not configured():
        return False
    delivery = store.claim_result_sync()
    if delivery is None:
        return False
    try:
        from finbot.experiments.archive_projection import build_archive

        archive = archive or PostgresArchive()
        kind, item = store.archive_entity(delivery["job_id"])
        if kind == "series_config":
            archive.save_series_config(item)
            store.finish_result_sync(delivery)
            return True
        if kind == "series":
            archive.save_series(item)
            store.finish_result_sync(delivery)
            return True
        job = item
        if job.get("series_id"):
            archive.save_series(store.get_series(job["series_id"]))
        events, after = [], 0
        while batch := store.events(job["id"], after=after, limit=500):
            events.extend(batch)
            after = batch[-1]["seq"]
        bundle = build_archive(job, events, submitted_by=archive.parameters.get("user"))
        archive.save(bundle)
    except Exception as exc:
        store.finish_result_sync(delivery, error=error_message(exc))
    else:
        store.finish_result_sync(delivery)
    return True
