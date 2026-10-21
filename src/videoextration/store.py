"""Local durable job queue and opaque media paths. No user filenames become paths."""

import json
import importlib
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .models import AuthProvider, BlobProvider, JobRepository


@dataclass
class Backend:
    store: JobRepository
    blobs: BlobProvider
    authenticate: AuthProvider | None = None
    enrich: Callable | None = None


def load_backend(settings: Settings, factory=None) -> Backend:
    """Factory(settings)->Backend. Set APP_BACKEND_FACTORY=package.module:callable."""
    target = os.getenv("APP_BACKEND_FACTORY", "")
    if factory is None and target:
        module, separator, attribute = target.partition(":")
        if not separator or not module or not attribute:
            raise ValueError("APP_BACKEND_FACTORY must be module:callable")
        factory = getattr(importlib.import_module(module), attribute)
    if factory is not None:
        return factory(settings)
    if settings.mode == "azure":
        raise ValueError("Azure mode requires an explicitly configured backend factory")
    return Backend(
        SQLiteStore(settings.data_dir / "jobs.sqlite3"),
        LocalBlobStore(settings.data_dir / "media"),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path_for(self, job_id: str, *, partial: bool = False) -> Path:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError("Invalid internal job ID")
        return self.root / f"{job_id}{'.part' if partial else '.media'}"

    def commit(self, job_id: str) -> None:
        os.replace(self.path_for(job_id, partial=True), self.path_for(job_id))
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def materialize(self, job_id: str) -> Path:
        return self.path_for(job_id)

    def discard(self, job_id: str) -> None:
        for partial in (True, False):
            self.path_for(job_id, partial=partial).unlink(missing_ok=True)

    def ready(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                    request TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    token_hash TEXT NOT NULL, expires REAL NOT NULL, token_used INTEGER NOT NULL DEFAULT 0,
                    actual_size INTEGER, sha256 TEXT, lease_until REAL, lease_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, report TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, lease_until, created_at)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["report"] = json.loads(result["report"]) if result["report"] else None
        return result

    def ready(self):
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def create(self, owner, request, token_hash, expires):
        job_id, now = str(uuid.uuid4()), utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO jobs(id, owner, state, request, created_at, updated_at, token_hash, expires)
                   VALUES(?, ?, 'awaiting-upload', ?, ?, ?, ?, ?)""",
                (job_id, owner, json.dumps(request), now, now, token_hash, expires),
            )
        return self.get(job_id, owner)

    def get(self, job_id, owner=None):
        with self.connect() as db:
            if owner is None:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
            return self.decode(row)

    def begin_upload(self, job_id, token_hash, now):
        with self.connect() as db:
            row = db.execute(
                """UPDATE jobs SET state='uploading', token_used=1, updated_at=?
                   WHERE id=? AND state='awaiting-upload' AND token_used=0
                   AND token_hash=? AND expires>? RETURNING *""",
                (utc_now(), job_id, token_hash, now),
            ).fetchone()
            return self.decode(row)

    def finish_upload(self, job_id, size, sha256):
        with self.connect() as db:
            changed = db.execute(
                """UPDATE jobs SET state='uploaded', actual_size=?, sha256=?, updated_at=?
                   WHERE id=? AND state='uploading'""", (size, sha256, utc_now(), job_id),
            ).rowcount
            if not changed:
                raise ValueError("Upload no longer active")

    def fail_upload(self, job_id):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='failed', updated_at=? WHERE id=? AND state='uploading'",
                (utc_now(), job_id),
            )

    def finalize(self, job_id, owner):
        with self.connect() as db:
            db.execute(
                """UPDATE jobs SET state='queued', updated_at=?
                   WHERE id=? AND owner=? AND state='uploaded'""",
                (utc_now(), job_id, owner),
            )
        return self.get(job_id, owner)

    def claim(self, lease_seconds):
        now, lease_id = time.time(), str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    """SELECT id FROM jobs WHERE state='queued'
                       OR (state='processing' AND lease_until<?)
                       ORDER BY created_at LIMIT 1""", (now,),
                ).fetchone()
                if row is None:
                    db.execute("COMMIT")
                    return None
                result = db.execute(
                    """UPDATE jobs SET state='processing', lease_until=?, lease_id=?,
                       attempts=attempts+1, updated_at=? WHERE id=? RETURNING *""",
                    (now + lease_seconds, lease_id, utc_now(), row["id"]),
                ).fetchone()
                db.execute("COMMIT")
                return self.decode(result)
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _finish(self, job_id, lease_id, report, state):
        with self.connect() as db:
            return bool(db.execute(
                """UPDATE jobs SET state=?, report=?, updated_at=?, lease_until=NULL, lease_id=NULL
                   WHERE id=? AND state='processing' AND lease_id=? AND lease_until>?""",
                (state, json.dumps(report, allow_nan=False), utc_now(), job_id, lease_id, time.time()),
            ).rowcount)

    def complete(self, job_id, lease_id, report):
        return self._finish(job_id, lease_id, report, "completed")

    def fail(self, job_id, lease_id, report):
        return self._finish(job_id, lease_id, report, "failed")
