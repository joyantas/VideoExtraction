from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from videoextration.azure_app import MAX_UPLOAD, create_app


class Auth:
    def authenticate(self, authorization):
        if authorization not in ("Bearer alice", "Bearer bob"):
            raise HTTPException(401, "Missing access token")
        return authorization


class Store:
    def __init__(self):
        self.jobs = {}
        self.enqueued = []
        self.size = 12
        self.mime = "video/mp4"
        self.fail_send = False

    def save(self, job, lease=None, create=False):
        self.jobs[job["id"]] = dict(job)

    def read(self, job_id, lease=None):
        from azure.core.exceptions import ResourceNotFoundError
        if job_id not in self.jobs:
            raise ResourceNotFoundError("not found")
        return dict(self.jobs[job_id])

    def signed_url(self, job_id, upload):
        return f"https://private.blob.core.windows.net/uploads/{job_id}?sp=c&spr=https"

    def upload_blob(self, job_id):
        return SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(
            size=self.size, content_settings=SimpleNamespace(content_type=self.mime), etag='"etag"',
        ))

    @contextmanager
    def locked(self, job_id):
        yield None

    def enqueue(self, job_id):
        from azure.core.exceptions import AzureError
        if self.fail_send:
            raise AzureError("test queue unavailable")
        self.enqueued.append(job_id)


@pytest.fixture
def cloud():
    store = Store()
    return TestClient(create_app(store=store, auth=Auth())), store


def create(client):
    return client.post("/api/jobs", headers={"Authorization": "Bearer alice"}, json={
        "filename": "campaign.mp4", "content_type": "video/mp4", "size_bytes": 12,
        "required_text": ["Campaign"],
    })


def test_cloud_authorization_and_owner_isolation(cloud):
    client, store = cloud
    assert client.post("/api/jobs", json={}).status_code == 401
    response = create(client)
    assert response.status_code == 201
    result = response.json()
    assert "owner" not in result
    assert result["upload_headers"]["If-None-Match"] == "*"
    assert client.get(f"/api/jobs/{result['id']}", headers={"Authorization": "Bearer bob"}).status_code == 404
    assert client.get(f"/api/jobs/{uuid4()}", headers={"Authorization": "Bearer alice"}).status_code == 404


def test_cloud_size_mismatch_rejected(cloud):
    client, store = cloud
    job_id = create(client).json()["id"]
    store.size = 13
    response = client.post(f"/api/jobs/{job_id}/complete", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 422
    assert not store.enqueued
    assert store.jobs[job_id]["status"] == "rejected"


def test_cloud_expired_upload_cannot_finalize(cloud):
    client, store = cloud
    job_id = create(client).json()["id"]
    store.jobs[job_id]["upload_expires_at"] = 0
    assert client.post(f"/api/jobs/{job_id}/complete", headers={"Authorization": "Bearer alice"}).status_code == 410
    assert not store.enqueued


def test_cloud_enqueue_failure_is_visible_and_retry_repairs_outbox(cloud):
    client, store = cloud
    job_id = create(client).json()["id"]
    store.fail_send = True
    url, headers = f"/api/jobs/{job_id}/complete", {"Authorization": "Bearer alice"}
    assert client.post(url, headers=headers).status_code == 503
    assert store.jobs[job_id]["status"] == "queue-pending"
    store.fail_send = False
    assert client.post(url, headers=headers).status_code == 202
    assert client.post(url, headers=headers).status_code == 202
    assert store.enqueued == [job_id]


def test_cloud_bounds_and_no_arbitrary_profile(cloud):
    client, _ = cloud
    headers = {"Authorization": "Bearer alice"}
    body = {"filename": "campaign.mp4", "content_type": "video/mp4", "size_bytes": MAX_UPLOAD + 1}
    assert client.post("/api/jobs", headers=headers, json=body).status_code == 422
    body.update(size_bytes=12, profile="official-current-platform-rules")
    assert client.post("/api/jobs", headers=headers, json=body).status_code == 422
