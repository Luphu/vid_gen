"""State store: jobs, transitions, external operations, budget, locks.

PostgreSQL in production. SQLite is accepted only for local dev/tests on a local disk
(config refuses it with APP_ENV=prod). SQL is written with ``?`` placeholders and a
portable subset of syntax; the PostgreSQL adapter rewrites them to ``%s``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .models import JobState


class ConcurrencyError(RuntimeError):
    """State changed under us (another runner or a stale view)."""


@dataclass
class Job:
    id: str
    idem_key: str
    state: JobState
    resume_state: JobState | None
    reason: str | None
    video_id: str | None
    dry_run: bool
    version: int
    created_at: float
    updated_at: float


class _Cursor:
    def __init__(self, cur: Any, pg: bool):
        self._cur, self._pg = cur, pg

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        self._cur.execute(sql.replace("?", "%s") if self._pg else sql, params)
        return self

    def fetchone(self) -> tuple | None:
        return self._cur.fetchone()

    def fetchall(self) -> list[tuple]:
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class Store:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg

            self._conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
        elif url.startswith("sqlite:///"):
            path = url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        else:
            raise ValueError("DATABASE_URL must be postgresql://… or sqlite:///…")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[_Cursor]:
        if self.pg:
            with self._conn.transaction(), self._conn.cursor() as cur:
                yield _Cursor(cur, True)
        else:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield _Cursor(cur, False)
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")

    def migrate(self) -> None:
        sql = resources.files("shorts_factory.migrations").joinpath("001_init.sql").read_text()
        sql = "\n".join(ln.split("--", 1)[0] for ln in sql.splitlines())  # drop comments before splitting
        with self.tx() as c:
            for stmt in (x.strip() for x in sql.split(";")):
                if stmt:
                    c.execute(stmt)

    # ---- locks -------------------------------------------------------------------------------
    def acquire_lock(self, name: str, owner: str, ttl_s: float) -> bool:
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO locks(name, owner, until) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                      (name, owner, now + ttl_s))
            row = c.execute("SELECT owner, until FROM locks WHERE name = ?", (name,)).fetchone()
            if row[0] == owner:
                c.execute("UPDATE locks SET until = ? WHERE name = ?", (now + ttl_s, name))
                return True
            if row[1] < now:  # expired lease: take over
                n = c.execute("UPDATE locks SET owner = ?, until = ? WHERE name = ? AND owner = ?",
                              (owner, now + ttl_s, name, row[0])).rowcount
                return n == 1
            return False

    def release_lock(self, name: str, owner: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))

    # ---- candidates --------------------------------------------------------------------------
    def upsert_candidate(self, video_id: str, data: dict, format_status: str,
                         ai_likelihood: float | None, fetched_at: float, expires_at: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO candidates(video_id, data, format_status, ai_likelihood, fetched_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (video_id) DO UPDATE SET data = excluded.data,"
                " format_status = excluded.format_status, ai_likelihood = excluded.ai_likelihood,"
                " fetched_at = excluded.fetched_at, expires_at = excluded.expires_at",
                (video_id, json.dumps(data, default=str), format_status, ai_likelihood, fetched_at, expires_at),
            )

    def get_candidate(self, video_id: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT data FROM candidates WHERE video_id = ?", (video_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def unused_candidates(self, now: float) -> list[dict]:
        with self.tx() as c:
            rows = c.execute(
                "SELECT data FROM candidates WHERE used_by_job IS NULL AND expires_at > ?", (now,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark_candidate_used(self, video_id: str, job_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE candidates SET used_by_job = ? WHERE video_id = ?", (job_id, video_id))

    def record_discovery(self, scope: dict, ids: list[str], expires_at: float) -> str:
        rid = uuid.uuid4().hex
        with self.tx() as c:
            c.execute("INSERT INTO discovery_runs(id, scope, candidate_ids, created_at, expires_at)"
                      " VALUES (?, ?, ?, ?, ?)",
                      (rid, json.dumps(scope, default=str), json.dumps(ids), time.time(), expires_at))
        return rid

    def purge_expired(self, now: float) -> dict[str, int]:
        """Delete YouTube API data past retention; keep only the video_id link on used jobs."""
        with self.tx() as c:
            a = c.execute("DELETE FROM candidates WHERE expires_at <= ? AND used_by_job IS NULL",
                          (now,)).rowcount
            b = c.execute("UPDATE candidates SET data = '{}' WHERE expires_at <= ? AND used_by_job IS NOT NULL"
                          " AND data <> '{}'", (now,)).rowcount
            d = c.execute("DELETE FROM discovery_runs WHERE expires_at <= ?", (now,)).rowcount
        return {"candidates_deleted": a, "candidates_scrubbed": b, "discovery_runs_deleted": d}

    # ---- jobs --------------------------------------------------------------------------------
    @staticmethod
    def _job(row: tuple) -> Job:
        return Job(row[0], row[1], JobState(row[2]), JobState(row[3]) if row[3] else None, row[4], row[5],
                   bool(row[6]), row[7], row[8], row[9])

    _JOB_COLS = "id, idem_key, state, resume_state, reason, video_id, dry_run, version, created_at, updated_at"

    def create_job(self, idem_key: str, video_id: str | None, dry_run: bool) -> tuple[Job, bool]:
        """Create a job or return the existing one with the same idempotency key."""
        now = time.time()
        jid = uuid.uuid4().hex[:16]
        with self.tx() as c:
            n = c.execute(
                "INSERT INTO jobs(id, idem_key, state, video_id, dry_run, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (idem_key) DO NOTHING",
                (jid, idem_key, JobState.DISCOVERED.value, video_id, int(dry_run), now, now),
            ).rowcount
            if n == 1:
                c.execute("INSERT INTO events(id, job_id, ts, from_state, to_state, note) VALUES (?,?,?,?,?,?)",
                          (uuid.uuid4().hex, jid, now, None, JobState.DISCOVERED.value, f"video {video_id}"))
            row = c.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE idem_key = ?", (idem_key,)).fetchone()
        return self._job(row), n == 1

    def get_job(self, job_id: str) -> Job | None:
        with self.tx() as c:
            row = c.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, limit: int = 20, states: list[JobState] | None = None) -> list[Job]:
        sql = f"SELECT {self._JOB_COLS} FROM jobs"
        params: tuple = ()
        if states:
            sql += " WHERE state IN (" + ",".join("?" * len(states)) + ")"
            params = tuple(s.value for s in states)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self.tx() as c:
            rows = c.execute(sql, (*params, limit)).fetchall()
        return [self._job(r) for r in rows]

    def count_jobs_since(self, since: float, exclude: tuple[JobState, ...] = (JobState.FAILED,)) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE created_at >= ?"
        if exclude:
            sql += " AND state NOT IN (" + ",".join("?" * len(exclude)) + ")"
        with self.tx() as c:
            row = c.execute(sql, (since, *(s.value for s in exclude))).fetchone()
        return int(row[0])

    def transition(self, job: Job, to: JobState, *, reason: str | None = None,
                   resume_state: JobState | None = None, stage: str | None = None,
                   payload: dict | None = None) -> Job:
        """Atomically move job to ``to`` iff it is still in ``job.state`` at ``job.version``.

        The completed stage result is written in the same transaction, so a crash either
        keeps the old state with no result or the new state with the result.
        """
        now = time.time()
        with self.tx() as c:
            n = c.execute(
                "UPDATE jobs SET state = ?, reason = ?, resume_state = ?, version = version + 1, updated_at = ?"
                " WHERE id = ? AND state = ? AND version = ?",
                (to.value, reason, resume_state.value if resume_state else None, now,
                 job.id, job.state.value, job.version),
            ).rowcount
            if n != 1:
                raise ConcurrencyError(f"job {job.id} changed concurrently (expected {job.state} v{job.version})")
            if stage is not None and payload is not None:
                c.execute("DELETE FROM stage_results WHERE job_id = ? AND stage = ?", (job.id, stage))
                c.execute("INSERT INTO stage_results(job_id, stage, payload, created_at) VALUES (?, ?, ?, ?)",
                          (job.id, stage, json.dumps(payload, default=str), now))
            c.execute("INSERT INTO events(id, job_id, ts, from_state, to_state, note) VALUES (?,?,?,?,?,?)",
                      (uuid.uuid4().hex, job.id, now, job.state.value, to.value, reason))
        job2 = self.get_job(job.id)
        assert job2 is not None
        return job2

    def save_stage(self, job_id: str, stage: str, payload: dict) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM stage_results WHERE job_id = ? AND stage = ?", (job_id, stage))
            c.execute("INSERT INTO stage_results(job_id, stage, payload, created_at) VALUES (?, ?, ?, ?)",
                      (job_id, stage, json.dumps(payload, default=str), time.time()))

    def get_stage(self, job_id: str, stage: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT payload FROM stage_results WHERE job_id = ? AND stage = ?",
                            (job_id, stage)).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, job_id: str) -> list[tuple]:
        with self.tx() as c:
            return c.execute("SELECT ts, from_state, to_state, note FROM events WHERE job_id = ? ORDER BY ts",
                             (job_id,)).fetchall()

    # ---- external operations -----------------------------------------------------------------
    def get_op(self, op_key: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT op_key, job_id, kind, status, request_id, artifact_key, detail"
                            " FROM external_ops WHERE op_key = ?", (op_key,)).fetchone()
        if not row:
            return None
        return dict(zip(("op_key", "job_id", "kind", "status", "request_id", "artifact_key", "detail"),
                        row, strict=True))

    def begin_op(self, op_key: str, job_id: str, kind: str) -> dict:
        """Insert a 'pending' row BEFORE the external call; returns the current row."""
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO external_ops(op_key, job_id, kind, status, created_at, updated_at)"
                      " VALUES (?, ?, ?, 'pending', ?, ?) ON CONFLICT DO NOTHING", (op_key, job_id, kind, now, now))
        op = self.get_op(op_key)
        assert op is not None
        return op

    def update_op(self, op_key: str, status: str, *, request_id: str | None = None,
                  artifact_key: str | None = None, detail: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE external_ops SET status = ?, request_id = COALESCE(?, request_id),"
                      " artifact_key = COALESCE(?, artifact_key), detail = ?, updated_at = ? WHERE op_key = ?",
                      (status, request_id, artifact_key, detail, time.time(), op_key))

    def ops_for_job(self, job_id: str) -> list[dict]:
        with self.tx() as c:
            rows = c.execute("SELECT op_key, kind, status, request_id, artifact_key, detail FROM external_ops"
                             " WHERE job_id = ? ORDER BY created_at", (job_id,)).fetchall()
        return [dict(zip(("op_key", "kind", "status", "request_id", "artifact_key", "detail"), r, strict=True))
                for r in rows]

    # ---- budget ------------------------------------------------------------------------------
    def reserve_budget(self, day: str, bucket: str, n: int, limit: int) -> bool:
        with self.tx() as c:
            c.execute("INSERT INTO budget_ledger(day, bucket, used) VALUES (?, ?, 0) ON CONFLICT DO NOTHING",
                      (day, bucket))
            return c.execute("UPDATE budget_ledger SET used = used + ? WHERE day = ? AND bucket = ?"
                             " AND used + ? <= ?", (n, day, bucket, n, limit)).rowcount == 1

    def budget_usage(self, day: str) -> dict[str, int]:
        with self.tx() as c:
            rows = c.execute("SELECT bucket, used FROM budget_ledger WHERE day = ?", (day,)).fetchall()
        return {b: u for b, u in rows}

    # ---- uploads -----------------------------------------------------------------------------
    _UP_COLS = ("job_id", "status", "session_uri_enc", "file_key", "file_size", "request", "video_id", "detail",
                "created_at")

    def get_upload(self, job_id: str) -> dict | None:
        with self.tx() as c:
            row = c.execute(f"SELECT {', '.join(self._UP_COLS)} FROM uploads WHERE job_id = ?",
                            (job_id,)).fetchone()
        return dict(zip(self._UP_COLS, row, strict=True)) if row else None

    def save_upload(self, job_id: str, **fields: Any) -> None:
        now = time.time()
        with self.tx() as c:
            exists = c.execute("SELECT 1 FROM uploads WHERE job_id = ?", (job_id,)).fetchone()
            if not exists:
                c.execute("INSERT INTO uploads(job_id, status, file_key, file_size, request, created_at, updated_at)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (job_id, fields.pop("status"), fields.pop("file_key"), fields.pop("file_size"),
                           fields.pop("request"), now, now))
            for k, v in fields.items():
                if k not in self._UP_COLS:
                    raise ValueError(k)
                c.execute(f"UPDATE uploads SET {k} = ?, updated_at = ? WHERE job_id = ?", (v, now, job_id))

    def uploads_since(self, since: float, statuses: tuple[str, ...] = ("uploaded", "processed")) -> int:
        with self.tx() as c:
            row = c.execute("SELECT COUNT(*) FROM uploads WHERE created_at >= ? AND status IN ("
                            + ",".join("?" * len(statuses)) + ")", (since, *statuses)).fetchone()
        return int(row[0])

    def any_completed_upload(self) -> bool:
        with self.tx() as c:
            row = c.execute("SELECT COUNT(*) FROM uploads WHERE video_id IS NOT NULL").fetchone()
        return int(row[0]) > 0
