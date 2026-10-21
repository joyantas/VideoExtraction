import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoextration.config import Settings
from videoextration.store import Backend, LocalBlobStore, SQLiteStore


@pytest.fixture(autouse=True)
def disable_paid_services(monkeypatch):
    monkeypatch.setenv("ENABLE_PAID_AI", "false")
    monkeypatch.setenv("SPEECH_ENDPOINT", "")


@pytest.fixture
def workspace():
    root = Path(".test-data") / str(uuid.uuid4())
    root.mkdir(parents=True, mode=0o700)
    try:
        yield root.resolve()
    finally:
        shutil.rmtree(root)
        try:
            root.parent.rmdir()
        except OSError:
            pass


@pytest.fixture
def settings(workspace):
    return Settings(data_dir=workspace, local_bearer_token="a" * 40, max_upload_bytes=16)


@pytest.fixture
def backend(settings):
    return Backend(
        SQLiteStore(settings.data_dir / "jobs.sqlite3"),
        LocalBlobStore(settings.data_dir / "media"),
    )


@pytest.fixture
def client(settings, backend):
    from videoextration.api import create_app

    def authenticate(token):
        identities = {"a" * 40: "owner-a", "b" * 40: "owner-b"}
        if token not in identities:
            raise ValueError("Invalid token")
        return identities[token]

    with TestClient(create_app(settings, backend_factory=lambda _: backend, authenticate=authenticate)) as client:
        yield client


@pytest.fixture
def auth():
    return {"Authorization": "Bearer " + "a" * 40}


@pytest.fixture
def request_body():
    return {
        "filename": "../../untrusted-name.mp4",
        "content_type": "video/mp4", "size_bytes": 4,
        "required_text": ["Sample heading"], "profile": "illustrative-portrait-v1",
    }


@pytest.fixture
def queued_job(client, auth, request_body):
    response = client.post("/api/jobs", json=request_body, headers=auth)
    assert response.status_code == 201
    job = response.json()
    assert client.put(job["upload_url"], content=b"test", headers=job["upload_headers"]).status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/complete", headers=auth).status_code == 202
    return job
