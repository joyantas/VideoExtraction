import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from videoextration.api import create_app
from videoextration.config import Settings
from videoextration.store import load_backend


def test_project_branding(client):
    assert client.app.title == "VideoExtraction"
    assert client.get("/openapi.json").json()["info"]["title"] == "VideoExtraction"
    page = client.get("/")
    assert page.status_code == 200
    assert "<h1>VideoExtraction</h1>" in page.text
    assert "<title>VideoExtraction " in page.text


def test_health_anonymous_and_direct(client):
    for path in ("/healthz", "/readyz"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers
    assert client.get("/healthz").json() == {"status": "ok"}


def test_authentication_required(client, request_body):
    assert client.post("/api/jobs", json=request_body).status_code == 401
    assert client.post("/api/jobs", json=request_body, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_unconfigured_local_identity_is_fail_closed(settings, backend, request_body):
    settings = replace(settings, local_bearer_token="")
    with TestClient(create_app(settings, backend_factory=lambda _: backend)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.post("/api/jobs", json=request_body, headers={"Authorization": "Bearer anything"}).status_code == 503


def test_local_token_never_allowed_in_azure():
    with pytest.raises(ValueError, match="forbidden"):
        Settings(mode="azure", local_bearer_token="a" * 40)
    with pytest.raises(ValueError, match="backend factory"):
        load_backend(Settings(mode="azure"))


def test_broken_dependency_not_ready(settings, backend):
    backend.store.ready = lambda: False
    with TestClient(create_app(settings, backend_factory=lambda _: backend)) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_create_and_owner_scope(client, auth, request_body):
    created = client.post("/api/jobs", json=request_body, headers=auth).json()
    job_id = created["id"]
    other = {"Authorization": "Bearer " + "b" * 40}
    assert client.get(f"/api/jobs/{job_id}", headers=auth).status_code == 200
    assert client.get(f"/api/jobs/{job_id}", headers=other).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/report", headers=other).status_code == 404
    assert client.post(f"/api/jobs/{job_id}/complete", headers=other).status_code == 404
    payload = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert "token_hash" not in payload and "owner" not in payload and "lease_id" not in payload
    assert created["upload"]["expires_at_epoch"] > time.time()


def test_finalize_before_upload_cannot_queue(client, auth, request_body, backend):
    created = client.post("/api/jobs", json=request_body, headers=auth).json()
    assert client.post(f"/api/jobs/{created['id']}/complete", headers=auth).status_code == 409
    assert backend.store.claim(180) is None
    assert client.get(f"/api/jobs/{created['id']}/report", headers=auth).status_code == 409


def test_upload_capability_single_use_and_scoped(client, auth, request_body, backend):
    first = client.post("/api/jobs", json=request_body, headers=auth).json()
    second = client.post("/api/jobs", json=request_body, headers=auth).json()
    assert client.put(second["upload_url"], content=b"test", headers=first["upload_headers"]).status_code == 403
    assert client.put(first["upload_url"], content=b"test").status_code == 403
    uploaded = client.put(first["upload_url"], content=b"test", headers=first["upload_headers"])
    assert uploaded.status_code == 200
    assert client.put(first["upload_url"], content=b"test", headers=first["upload_headers"]).status_code == 403
    assert backend.blobs.path_for(first["id"]).read_bytes() == b"test"
    assert backend.store.get(first["id"])["state"] == "uploaded"
    assert backend.store.claim(180) is None
    for _ in range(2):
        assert client.post(f"/api/jobs/{first['id']}/complete", headers=auth).status_code == 202
    assert backend.store.claim(180)["id"] == first["id"]
    assert backend.store.claim(180) is None


def test_expired_capability(client, auth, request_body, backend):
    created = client.post("/api/jobs", json=request_body, headers=auth).json()
    with backend.store.connect() as db:
        db.execute("UPDATE jobs SET expires=0 WHERE id=?", (created["id"],))
    assert client.put(created["upload_url"], content=b"test", headers=created["upload_headers"]).status_code == 403


def test_create_size_type_and_required_text_validation(client, auth, request_body):
    assert client.post("/api/jobs", json={**request_body, "size_bytes": 17}, headers=auth).status_code == 413
    assert client.post("/api/jobs", json={**request_body, "size_bytes": 0}, headers=auth).status_code == 422
    assert client.post("/api/jobs", json={**request_body, "content_type": "text/plain"}, headers=auth).status_code == 415
    assert client.post("/api/jobs", json={**request_body, "required_text": [""]}, headers=auth).status_code == 422
    assert client.post("/api/jobs", json={**request_body, "required_text": ["x" * 301]}, headers=auth).status_code == 422
    assert client.post("/api/jobs", json={**request_body, "profile": "approved"}, headers=auth).status_code == 422


def test_oversize_stream_is_removed_and_token_spent(client, auth, request_body, backend):
    created = client.post("/api/jobs", json=request_body, headers=auth).json()
    response = client.put(created["upload_url"], content=iter([b"test", b"too-much"]), headers=created["upload_headers"])
    assert response.status_code == 413
    assert backend.store.get(created["id"])["state"] == "failed"
    assert not backend.blobs.path_for(created["id"], partial=True).exists()
    assert not backend.blobs.path_for(created["id"]).exists()
    assert client.put(created["upload_url"], content=b"test", headers=created["upload_headers"]).status_code == 403
    assert client.post(f"/api/jobs/{created['id']}/complete", headers=auth).status_code == 409


@pytest.mark.parametrize("body,content_type,expected", [
    (b"tes", "video/mp4", 400),
    (b"test", "text/plain", 415),
    (b"test-is-too-long", "video/mp4", 413),
])
def test_upload_mismatch_fails_closed(client, auth, request_body, body, content_type, expected):
    created = client.post("/api/jobs", json=request_body, headers=auth).json()
    headers = {**created["upload_headers"], "Content-Type": content_type}
    assert client.put(created["upload_url"], content=body, headers=headers).status_code == expected


def test_ui_security_and_memory_only_token(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    script = client.get("/static/app.js").text
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "document.createTextNode" in script
