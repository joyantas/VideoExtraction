"""Azure API: validated Entra access tokens, direct create-only browser uploads."""

import logging
import time
from pathlib import Path
from uuid import UUID, uuid4

from azure.core.exceptions import AzureError, ResourceNotFoundError
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import EntraAuth
from .azure_store import AzureStore, utc_now
from .config import Settings

logger = logging.getLogger(__name__)
MAX_UPLOAD = 100 * 1024 * 1024


class CloudJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    content_type: str
    size_bytes: int = Field(gt=0)
    profile: str = "illustrative-portrait-v1"
    required_text: list[str] = Field(default_factory=list, max_length=20)


def create_app(store=None, auth=None) -> FastAPI:
    store = store or AzureStore()
    auth = auth or EntraAuth()
    settings = Settings.from_env()
    app = FastAPI(title="VideoExtration", docs_url=None, redoc_url=None, openapi_url=None)
    assets = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=assets), name="static")

    @app.exception_handler(AzureError)
    async def azure_error(request, exc):
        logger.error("Azure storage request failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Azure storage unavailable; retry the operation."})

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://*.blob.core.windows.net; frame-ancestors 'none'; base-uri 'none'",
        })
        return response

    def owner(authorization: str | None = Header(default=None)) -> str:
        return auth.authenticate(authorization)

    def owned(job_id: UUID, principal: str) -> dict:
        try:
            job = store.read(str(job_id))
        except ResourceNotFoundError as exc:
            raise HTTPException(404, "Job not found.") from exc
        if job["owner"] != principal:
            raise HTTPException(404, "Job not found.")
        return job

    def public(job: dict) -> dict:
        return {key: value for key, value in job.items() if key not in ("owner", "upload_etag")}

    @app.get("/")
    def index():
        return FileResponse(assets / "index.html")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        try:
            store.blobs.get_container_client("jobs").get_container_properties()
            store.queue.get_queue_properties()
        except AzureError as exc:
            logger.error("Storage readiness failed: %s", type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "not-ready"})
        return {"status": "ready", "ai": "optional; unconfigured checks are not evaluated"}

    @app.post("/api/jobs", status_code=201)
    def create_job(body: CloudJobRequest, principal: str = Depends(owner)):
        if body.size_bytes > settings.max_upload_bytes:
            raise HTTPException(422, "Upload exceeds the configured size limit.")
        if body.content_type not in ("video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo"):
            raise HTTPException(422, "Unsupported video content type.")
        if body.profile != "illustrative-portrait-v1":
            raise HTTPException(422, "Unknown illustrative placement profile.")
        if any(not text.strip() or len(text) > 500 for text in body.required_text):
            raise HTTPException(422, "Mandatory text entries must contain 1–500 characters.")
        job = {
            **body.model_dump(), "id": str(uuid4()), "owner": principal, "status": "awaiting-upload",
            "created_at": utc_now(), "upload_expires_at": time.time() + 600,
        }
        store.save(job, create=True)
        return {
            **public(job), "upload_url": store.signed_url(job["id"], upload=True),
            "upload_headers": {"x-ms-blob-type": "BlockBlob", "Content-Type": body.content_type, "If-None-Match": "*"},
        }

    @app.post("/api/jobs/{job_id}/complete", status_code=202)
    def complete(job_id: UUID, principal: str = Depends(owner)):
        owned(job_id, principal)
        with store.locked(str(job_id)) as lease:
            job = store.read(str(job_id), lease=lease)
            if job["status"] in ("queued", "processing", "completed", "failed"):
                return public(job)
            if job["status"] == "awaiting-upload":
                if time.time() > job["upload_expires_at"]:
                    raise HTTPException(410, "Upload authorization expired; create a new job.")
                try:
                    properties = store.upload_blob(job["id"]).get_blob_properties()
                except ResourceNotFoundError as exc:
                    raise HTTPException(409, "Upload is not present.") from exc
                if properties.size != job["size_bytes"] or properties.content_settings.content_type != job["content_type"]:
                    job["status"] = "rejected"
                    job["error"] = "Upload size or media content type does not match the job."
                    store.save(job, lease=lease)
                    raise HTTPException(422, job["error"])
                job["upload_etag"] = properties.etag
                job["status"] = "queue-pending"
                store.save(job, lease=lease)
            if job["status"] != "queue-pending":
                raise HTTPException(409, "Job cannot be finalized in its current state.")
            # Persist outbox intent before delivery. Retrying /complete repairs a failed send.
            store.enqueue(job["id"])
            job["status"] = "queued"
            store.save(job, lease=lease)
            return public(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: UUID, principal: str = Depends(owner)):
        return public(owned(job_id, principal))

    @app.get("/api/jobs/{job_id}/report")
    def get_report(job_id: UUID, principal: str = Depends(owner)):
        job = owned(job_id, principal)
        if job["status"] != "completed" and not job.get("report_available"):
            raise HTTPException(409, "No completed report is available; inspect job status/error.")
        return store.read_report(str(job_id))

    return app
