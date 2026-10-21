"""One queue message per Container Apps Job execution; bounded at-least-once processing."""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from azure.core.exceptions import AzureError, HttpResponseError

from .ai import AISettings, AzureAI
from .azure_store import AzureStore, utc_now
from .media import MediaError
from .rules import evaluate

logger = logging.getLogger(__name__)


def process_one(store=None, analyze=None) -> bool:
    store = store or AzureStore()
    if analyze is None:
        from .worker import analyze_video
        analyze = analyze_video
    messages = store.queue.receive_messages(messages_per_page=1, visibility_timeout=1800)
    message = next(iter(messages), None)
    if message is None:
        return False
    job = None
    try:
        job_id = str(UUID(message.content))
    except (ValueError, AttributeError) as exc:
        logger.error("Invalid queue message: %s", type(exc).__name__)
        store.poison.send_message("Malformed job identifier; inspect queue diagnostics.")
        store.queue.delete_message(message)
        return True

    try:
        lease = store.record_blob(job_id).acquire_lease(lease_duration=60)
    except HttpResponseError as exc:
        if exc.status_code == 409:
            logger.info("Job already leased; retaining queue message for retry.")
            return True
        if exc.status_code == 404:
            logger.error("Queue message references a missing job; moving to poison queue.")
            store.poison.send_message(job_id)
            store.queue.delete_message(message)
            return True
        raise
    stopped = threading.Event()
    lease_errors: list[AzureError] = []

    def renew():
        while not stopped.wait(20):
            try:
                lease.renew()
            except AzureError as exc:
                logger.error("Job lease renewal failed: %s", type(exc).__name__)
                lease_errors.append(exc)
                return

    renewer = threading.Thread(target=renew, daemon=True)
    renewer.start()
    try:
        job = store.read(job_id, lease=lease)
        if job["status"] in ("completed", "failed", "rejected"):
            store.queue.delete_message(message)
            return True
        if job["status"] not in ("queued", "queue-pending", "processing"):
            logger.error("Queue message references a job that has not finalized.")
            store.poison.send_message(job_id)
            store.queue.delete_message(message)
            return True
        if message.dequeue_count > 3:
            job.update(status="failed", error="Queue retry limit exceeded; inspect analysis-poison.", updated_at=utc_now())
            store.save(job, lease=lease)
            store.poison.send_message(job_id)
            store.queue.delete_message(message)
            return True
        job.update(status="processing", updated_at=utc_now())
        store.save(job, lease=lease)
        with tempfile.TemporaryDirectory(prefix="videoextraction-") as directory:
            root = Path(directory)
            source = root / "source.video"
            store.download(job, source)
            report, frames = analyze(source, job["required_text"])
            settings = AISettings.from_env()
            enrichment = AzureAI(settings, store.credential).collect(
                store.signed_url(job_id, upload=False)
                if settings.enabled and settings.understanding_endpoint else None,
                frames, job["required_text"], recognized_transcript=report.get("transcript", {}).get("text"),
            )
            apply_enrichment(report, enrichment)
            report["job_id"] = job_id
            if lease_errors:
                raise RuntimeError("Lost job lease; refusing to persist a competing analysis result.")
            store.report(job_id, report)
        job.update(status="completed", updated_at=utc_now())
        store.save(job, lease=lease)
        store.queue.delete_message(message)
        return True
    except MediaError as exc:
        logger.error("Media analysis rejected job: %s", exc.code)
        if job is not None and not lease_errors:
            report = evaluate(None, [], job["required_text"], error_code=exc.code)
            report["job_id"] = job_id
            store.report(job_id, report)
            job.update(status="failed", error=exc.code, report_available=True, updated_at=utc_now())
            store.save(job, lease=lease)
            store.queue.delete_message(message)
        return True
    except (AzureError, RuntimeError, ValueError, OSError) as exc:
        logger.error("Queue analysis failed: %s", type(exc).__name__, exc_info=False)
        if job is not None and not lease_errors:
            job.update(last_error=type(exc).__name__, updated_at=utc_now())
            store.save(job, lease=lease)
        # Queue visibility expiry retries the durable message. Never manufacture a report.
        raise
    finally:
        stopped.set()
        renewer.join(timeout=5)
        if not lease_errors:
            lease.release()


def apply_enrichment(report: dict, enrichment: dict) -> dict:
    from .worker import _conservative_summary
    mapping = {
        "campaign_metadata": "campaign-metadata", "mandatory_text": "required-text",
        "image_safety": "frame-safety", "text_safety": "transcript-safety",
    }
    replace_ids = set(mapping.values())
    report["findings"] = [finding for finding in report["findings"] if finding["rule_id"] not in replace_ids]
    report["evidence"].append({"id": "azure-ai", "kind": "service-output-and-generated-interpretations", "observations": enrichment["evidence"]})
    report["findings"].extend({
        "rule_id": mapping.get(check["id"], check["id"]), "state": check["state"],
        "conclusion": check["detail"], "evidence_ids": ["azure-ai"],
    } for check in enrichment["checks"])
    report["coverage"]["ocr_configured"] = bool(enrichment["evidence"].get("ocr"))
    report["coverage"]["ai_verified"] = False
    return _conservative_summary(report)


def main():
    logging.basicConfig(level=logging.INFO)
    process_one()


if __name__ == "__main__":
    main()
