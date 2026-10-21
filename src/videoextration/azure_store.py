"""Private Blob-backed job records and at-least-once Storage Queue delivery."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from azure.core import MatchConditions
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, ContentSettings, generate_blob_sas
from azure.storage.queue import QueueClient


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AzureStore:
    def __init__(self):
        self.account = os.environ["AZURE_STORAGE_ACCOUNT"]
        if not self.account.isalnum() or not 3 <= len(self.account) <= 24:
            raise ValueError("AZURE_STORAGE_ACCOUNT must be the storage account name, not a URL.")
        self.credential = ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID"))
        self.blobs = BlobServiceClient(
            f"https://{self.account}.blob.core.windows.net", credential=self.credential,
            connection_timeout=10, read_timeout=30, retry_total=2,
        )
        self.queue = QueueClient(
            f"https://{self.account}.queue.core.windows.net", "analysis",
            credential=self.credential, connection_timeout=10, read_timeout=30, retry_total=2,
        )
        self.poison = QueueClient(
            f"https://{self.account}.queue.core.windows.net", "analysis-poison",
            credential=self.credential, connection_timeout=10, read_timeout=30, retry_total=2,
        )

    def record_blob(self, job_id: str):
        return self.blobs.get_blob_client("jobs", f"{UUID(job_id)}.json")

    def upload_blob(self, job_id: str):
        return self.blobs.get_blob_client("uploads", f"{UUID(job_id)}.video")

    def read(self, job_id: str, lease=None) -> dict:
        raw = self.record_blob(job_id).download_blob(lease=lease, max_concurrency=1).readall()
        if len(raw) > 128 * 1024:
            raise ValueError("Job record exceeds its bounded schema.")
        return json.loads(raw)

    def save(self, job: dict, lease=None, create: bool = False):
        self.record_blob(job["id"]).upload_blob(
            json.dumps(job), overwrite=not create, lease=lease,
            content_settings=ContentSettings(content_type="application/json"),
        )

    @contextmanager
    def locked(self, job_id: str):
        lease = self.record_blob(job_id).acquire_lease(lease_duration=60)
        try:
            yield lease
        finally:
            lease.release()

    def signed_url(self, job_id: str, *, upload: bool) -> str:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(minutes=10 if upload else 20)
        delegation = self.blobs.get_user_delegation_key(now - timedelta(minutes=1), expiry)
        signature = generate_blob_sas(
            self.account, "uploads", f"{UUID(job_id)}.video",
            user_delegation_key=delegation,
            permission=BlobSasPermissions(create=True) if upload else BlobSasPermissions(read=True),
            start=now - timedelta(minutes=1), expiry=expiry, protocol="https",
        )
        return f"{self.upload_blob(job_id).url}?{signature}"

    def enqueue(self, job_id: str):
        self.queue.send_message(str(UUID(job_id)), time_to_live=86400)

    def report(self, job_id: str, report: dict):
        self.blobs.get_blob_client("reports", f"{UUID(job_id)}.json").upload_blob(
            json.dumps(report), overwrite=True, content_settings=ContentSettings(content_type="application/json"),
        )

    def read_report(self, job_id: str) -> dict:
        return json.loads(self.blobs.get_blob_client("reports", f"{UUID(job_id)}.json").download_blob().readall())

    def download(self, job: dict, target):
        # The worker rejects bytes changed after finalization. Browsers only get create permission.
        downloader = self.upload_blob(job["id"]).download_blob(
            etag=job["upload_etag"], match_condition=MatchConditions.IfNotModified, max_concurrency=1,
        )
        size = 0
        with target.open("xb") as output:
            for chunk in downloader.chunks():
                size += len(chunk)
                if size > job["size_bytes"]:
                    raise ValueError("Downloaded media exceeds the finalized size.")
                output.write(chunk)
        if size != job["size_bytes"]:
            raise ValueError("Downloaded media does not match the finalized size.")
