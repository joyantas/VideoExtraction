import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from videoextration.store import SQLiteStore


def test_database_survives_reopen(queued_job, backend):
    reopened = SQLiteStore(backend.store.path)
    assert reopened.get(queued_job["id"])["state"] == "queued"


def test_claim_is_atomic_and_old_lease_is_fenced(queued_job, backend):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: backend.store.claim(180), range(4)))
    claimed = [value for value in results if value is not None]
    assert len(claimed) == 1
    old_lease = claimed[0]["lease_id"]
    with backend.store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, queued_job["id"]))
    retried = backend.store.claim(180)
    assert retried["lease_id"] != old_lease
    assert retried["attempts"] == 2
    assert not backend.store.complete(queued_job["id"], old_lease, {"stale": True})
    assert backend.store.complete(queued_job["id"], retried["lease_id"], {"complete": False})
    assert backend.store.get(queued_job["id"])["report"] == {"complete": False}


def test_local_paths_ignore_user_filename(queued_job, backend):
    path = backend.blobs.materialize(queued_job["id"])
    assert path.parent == backend.blobs.root
    assert path.name == f"{queued_job['id']}.media"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == backend.store.get(queued_job["id"])["sha256"]
    with pytest.raises(ValueError):
        backend.blobs.path_for("../../escape")
