"""HTTP API. Cloud adapters are selected explicitly through APP_BACKEND_FACTORY."""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .models import ALLOWED_CONTENT_TYPES, CreateJob
from .store import load_backend

log = logging.getLogger(__name__)


def public_job(job):
    return {
        "id": job["id"], "status": job["state"], "state": job["state"], "created_at": job["created_at"],
        "updated_at": job["updated_at"], "request": job["request"],
        "size_bytes": job.get("actual_size"), "sha256": job.get("sha256"),
        "attempts": job.get("attempts", 0),
    }


def create_app(settings=None, *, backend_factory=None, authenticate=None):
    settings = settings or Settings.from_env()
    if settings.mode == "azure" and backend_factory is None and authenticate is None:
        from .azure_app import create_app as create_azure_app
        return create_azure_app()
    backend = load_backend(settings, backend_factory)
    authenticate = authenticate or backend.authenticate
    if settings.mode == "azure" and authenticate is None:
        raise ValueError("Azure mode requires a verified identity provider")
    store, blobs = backend.store, backend.blobs
    app = FastAPI(title="VideoExtraction", version="0.1.0")
    app.state.backend = backend
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type", "X-Upload-Token"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    def owner(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization[7:]
        if not token or len(token) > 16384:
            raise HTTPException(401, "Invalid bearer token")
        if authenticate is not None:
            try:
                identity = authenticate(token)
                if not isinstance(identity, str) or not identity:
                    raise ValueError("Missing owner")
                return identity
            except ValueError:
                raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        if settings.mode != "local" or not settings.local_bearer_token:
            raise HTTPException(503, "Identity provider is not configured")
        if not hmac.compare_digest(token.encode(), settings.local_bearer_token.encode()):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        return settings.local_owner

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            ready = bool(store.ready() and blobs.ready() and (
                authenticate is not None or (settings.mode == "local" and settings.local_bearer_token)
            ))
        except (sqlite3.Error, OSError) as exc:
            log.error("Local dependency readiness failed: %s", type(exc).__name__)
            ready = False
        return JSONResponse({"status": "ready" if ready else "not-ready"}, status_code=200 if ready else 503)

    @app.post("/api/jobs", status_code=201)
    def create(request: CreateJob, identity: str = Depends(owner)):
        if request.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(415, "Unsupported video content type")
        if request.size_bytes > settings.max_upload_bytes:
            raise HTTPException(413, "Upload exceeds configured size limit")
        if any(not text.strip() or len(text) > 300 for text in request.required_text):
            raise HTTPException(422, "Required text entries must contain 1–300 characters")
        token = secrets.token_urlsafe(32)
        expires = time.time() + settings.upload_ttl_seconds
        job = store.create(
            identity, request.model_dump(), hashlib.sha256(token.encode()).hexdigest(), expires,
        )
        return {
            "id": job["id"], "status": job["state"],
            "upload_url": f"/api/uploads/{job['id']}",
            "upload_headers": {"X-Upload-Token": token, "Content-Type": request.content_type},
            "job": public_job(job),
            "upload": {
                "url": f"/api/uploads/{job['id']}", "method": "PUT",
                "token": token, "token_header": "X-Upload-Token",
                "expires_at_epoch": expires, "max_bytes": settings.max_upload_bytes,
            },
        }

    @app.put("/api/uploads/{job_id}")
    async def upload(job_id: str, request: Request, x_upload_token: str | None = Header(default=None)):
        if not x_upload_token or len(x_upload_token) > 512:
            raise HTTPException(403, "Invalid or expired upload capability")
        job = store.begin_upload(job_id, hashlib.sha256(x_upload_token.encode()).hexdigest(), time.time())
        if job is None:
            raise HTTPException(403, "Invalid, expired, or consumed upload capability")
        try:
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != job["request"]["content_type"]:
                raise HTTPException(415, "Content type must match the job declaration")
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    raise HTTPException(400, "Invalid Content-Length")
                if length < 0:
                    raise HTTPException(400, "Invalid Content-Length")
                if length > settings.max_upload_bytes or length > job["request"]["size_bytes"]:
                    raise HTTPException(413, "Upload exceeds declared or configured size")
                if length != job["request"]["size_bytes"]:
                    raise HTTPException(400, "Content-Length must match declared size")
            size, digest = 0, hashlib.sha256()
            async with asyncio.timeout(settings.upload_wall_seconds):
                with blobs.path_for(job_id, partial=True).open("xb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > settings.max_upload_bytes or size > job["request"]["size_bytes"]:
                            raise HTTPException(413, "Upload exceeds declared or configured size")
                        output.write(chunk)
                        digest.update(chunk)
                    if size != job["request"]["size_bytes"]:
                        raise HTTPException(400, "Upload size does not match declaration")
                    output.flush()
                    os.fsync(output.fileno())
            blobs.commit(job_id)
            store.finish_upload(job_id, size, digest.hexdigest())
            return {"id": job_id, "state": "uploaded", "size_bytes": size, "sha256": digest.hexdigest()}
        except BaseException as exc:
            try:
                blobs.discard(job_id)
            finally:
                store.fail_upload(job_id)
            if isinstance(exc, TimeoutError):
                raise HTTPException(408, "Upload time limit exceeded")
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, (OSError, sqlite3.Error)):
                log.error("Upload persistence failed: %s", type(exc).__name__)
                raise HTTPException(503, "Upload persistence failed; create a new job to retry") from exc
            raise

    @app.post("/api/jobs/{job_id}/complete", status_code=202)
    @app.post("/api/jobs/{job_id}/finalize", status_code=202, include_in_schema=False)
    def finalize(job_id: str, identity: str = Depends(owner)):
        job = store.finalize(job_id, identity)
        if job is None:
            raise HTTPException(404, "Job not found")
        if job["state"] not in {"queued", "processing", "completed"}:
            raise HTTPException(409, "Job must have a complete upload before finalization")
        return public_job(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, identity: str = Depends(owner)):
        job = store.get(job_id, identity)
        if job is None:
            raise HTTPException(404, "Job not found")
        return public_job(job)

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str, identity: str = Depends(owner)):
        job = store.get(job_id, identity)
        if job is None:
            raise HTTPException(404, "Job not found")
        if job["report"] is None:
            raise HTTPException(409, "Report is not available")
        return job["report"]

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
