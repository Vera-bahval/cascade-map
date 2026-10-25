"""SQLite queue with immutable job snapshots and encrypted credentials."""

from __future__ import annotations
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def stamp():
    from datetime import datetime, UTC

    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path=None):
        from finbot.config import settings

        self.path = Path(
            path or os.environ.get("FINBOT_EXPERIMENTS_DB", settings.FINBOT_EXPERIMENTS_DB)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path = Path(str(self.path) + ".key")
        self._init()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS configs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                worker_id TEXT, idempotency_key TEXT, updated REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,updated);
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(owner,idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, data TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def cipher(self):
        if not self.key_path.exists():
            try:
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
                    f.flush()
                    os.fsync(f.fileno())
            except FileExistsError:
                pass
        for _ in range(20):
            key = self.key_path.read_bytes()
            if len(key) == 44:
                return Fernet(key)
            time.sleep(0.01)
        raise RuntimeError("Не удалось прочитать ключ хранилища подключений")

    def _seal(self, data):
        result = dict(data)
        key = result.pop("api_key", "")
        result["has_api_key"] = bool(key)
        result["credential"] = self.cipher().encrypt(key.encode()).decode() if key else ""
        return result

    def _connection(self, data, secret=False):
        result = dict(data)
        credential = result.pop("credential", "")
        if secret:
            result["api_key"] = (
                self.cipher().decrypt(credential.encode()).decode() if credential else ""
            )
        return result

    def save_connection(self, owner, data):
        item = {**self._seal(data), "id": uuid.uuid4().hex, "created_at": stamp()}
        with self.db() as db:
            db.execute(
                "INSERT INTO connections VALUES (?,?,?)", (item["id"], owner, json.dumps(item))
            )
        return self._connection(item)

    def get_connection(self, owner, ident, secret=False):
        with self.db() as db:
            row = db.execute(
                "SELECT data FROM connections WHERE id=? AND owner=?", (ident, owner)
            ).fetchone()
        if not row:
            raise KeyError("Подключение не найдено")
        return self._connection(json.loads(row[0]), secret)

    def list_connections(self, owner):
        with self.db() as db:
            rows = db.execute("SELECT data FROM connections WHERE owner=?", (owner,)).fetchall()
        return [self._connection(json.loads(r[0])) for r in rows]

    def save_config(self, owner, data):
        self.get_connection(owner, data["connection_id"])
        item = {**data, "id": uuid.uuid4().hex, "created_at": stamp()}
        with self.db() as db:
            db.execute("INSERT INTO configs VALUES (?,?,?)", (item["id"], owner, json.dumps(item)))
        return item

    def get_config(self, owner, ident):
        with self.db() as db:
            row = db.execute(
                "SELECT data FROM configs WHERE id=? AND owner=?", (ident, owner)
            ).fetchone()
        if not row:
            raise KeyError("Конфигурация не найдена")
        return json.loads(row[0])

    def list_configs(self, owner):
        with self.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM configs WHERE owner=?", (owner,)).fetchall()
            ]

    def create_job(
        self,
        owner,
        kind,
        config,
        cases,
        *,
        code_revision="unknown",
        scenario_hash="",
        idempotency_key=None,
    ):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = db.execute(
                    "SELECT data FROM jobs WHERE owner=? AND idempotency_key=?",
                    (owner, idempotency_key),
                ).fetchone()
                if existing:
                    return self._public_job(json.loads(existing[0]))
            row = db.execute(
                "SELECT data FROM connections WHERE owner=? AND id=?",
                (owner, config["connection_id"]),
            ).fetchone()
            if not row:
                raise KeyError("Подключение не найдено")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('queued','running')",
                    (owner,),
                ).fetchone()[0]
                >= 20
            ):
                raise ValueError("В очереди уже 20 заданий")
            job = {
                "id": uuid.uuid4().hex,
                "owner": owner,
                "kind": kind,
                "status": "queued",
                "created_at": stamp(),
                "updated_at": stamp(),
                "config": config,
                "connection": json.loads(row[0]),
                "cases": cases,
                "code_revision": code_revision,
                "scenario_hash": scenario_hash,
                "cancel_requested": False,
                "metrics": {},
            }
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (job["id"], owner, "queued", None, idempotency_key, time.time(), json.dumps(job)),
            )
        return self._public_job(job)

    def _public_job(self, job, include_secret=False):
        result = dict(job)
        result["connection"] = self._connection(job["connection"], include_secret)
        result.pop("owner", None) if not include_secret else None
        return result

    def get_job(self, job_id, owner=None, include_secret=False):
        with self.db() as db:
            row = db.execute("SELECT owner,data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or (owner is not None and row["owner"] != owner):
                raise KeyError("Запуск не найден")
            result = self._public_job(json.loads(row["data"]), include_secret)
        return result

    def list_jobs(self, owner, limit=200):
        with self.db() as db:
            rows = db.execute(
                "SELECT data FROM jobs WHERE owner=? ORDER BY updated DESC LIMIT ?", (owner, limit)
            ).fetchall()
        result = []
        for row in rows:
            job = self._public_job(json.loads(row[0]))
            job["case_count"] = len(job["cases"])
            job["finished_cases"] = sum(c["status"] in TERMINAL for c in job["cases"])
            job.pop("cases")
            result.append(job)
        return result

    def _write_job(self, db, job):
        job["updated_at"] = stamp()
        db.execute(
            "UPDATE jobs SET status=?,worker_id=?,updated=?,data=? WHERE id=?",
            (
                job["status"],
                job.get("worker_id"),
                time.time(),
                json.dumps(job, allow_nan=False),
                job["id"],
            ),
        )

    def update_job(self, job_id, **fields):
        forbidden = {
            "id",
            "owner",
            "connection",
            "config",
            "code_revision",
            "scenario_hash",
        } & fields.keys()
        if forbidden:
            raise ValueError("Снимок конфигурации неизменяем")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = json.loads(row[0])
            job.update(fields)
            self._write_job(db, job)

    def update_case(self, job_id, case_id, **fields):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = json.loads(row[0])
            for case in job["cases"]:
                if case["id"] == case_id:
                    case.update(fields)
                    break
            else:
                raise KeyError(case_id)
            self._write_job(db, job)

    def append_event(self, job_id, event):
        with self.db() as db:
            db.execute(
                "INSERT INTO events(job_id,data) VALUES (?,?)",
                (job_id, json.dumps(event, default=str)),
            )

    def events(self, job_id, after=0, limit=500):
        with self.db() as db:
            rows = db.execute(
                "SELECT seq,data FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, after, limit),
            ).fetchall()
        return [{"seq": r[0], **json.loads(r[1])} for r in rows]

    def claim_job(self, worker_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM jobs WHERE status='queued' ORDER BY updated LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(status="running", worker_id=worker_id, started_at=stamp())
            self._write_job(db, job)
            return self._public_job(job, True)

    def heartbeat(self, worker_id):
        with self.db() as db:
            db.execute(
                "INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET seen=excluded.seen",
                (worker_id, time.time()),
            )

    def worker_status(self):
        with self.db() as db:
            seen = db.execute("SELECT MAX(seen) FROM workers").fetchone()[0]
        return {"online": seen is not None and time.time() - seen < 45, "last_seen": seen}

    def cancelled(self, job_id):
        return bool(self.get_job(job_id).get("cancel_requested"))

    def cancel(self, owner, job_id):
        self.get_job(job_id, owner)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(
                db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            )
            if job["status"] not in TERMINAL:
                job["cancel_requested"] = True
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                    for case in job["cases"]:
                        case["status"] = "cancelled"
                self._write_job(db, job)
        return self._public_job(job)

    def _interrupt(self, db, row):
        job = json.loads(row[0])
        job.update(
            status="interrupted", error="Исполнитель остановлен. Автоматический повтор отключён."
        )
        for case in job["cases"]:
            if case["status"] not in TERMINAL:
                case["status"] = "interrupted"
        self._write_job(db, job)

    def recover_stale_jobs(self, seconds=90):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT j.data FROM jobs j LEFT JOIN workers w ON j.worker_id=w.id WHERE j.status='running' AND (w.seen IS NULL OR w.seen<?)",
                (time.time() - seconds,),
            ).fetchall()
            for row in rows:
                self._interrupt(db, row)
        return len(rows)

    def finish_interrupted(self, worker_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT data FROM jobs WHERE worker_id=? AND status='running'", (worker_id,)
            ).fetchall():
                self._interrupt(db, row)
            db.execute("DELETE FROM workers WHERE id=?", (worker_id,))
