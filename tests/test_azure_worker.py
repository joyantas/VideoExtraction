from types import SimpleNamespace
from uuid import uuid4

import pytest

from videoextration.azure_worker import process_one
from videoextration.media import MediaError
from videoextration.rules import evaluate


class Queue:
    def __init__(self, message=None):
        self.message = message
        self.deleted = []
        self.sent = []

    def receive_messages(self, **kwargs):
        assert kwargs == {"messages_per_page": 1, "visibility_timeout": 1800}
        return iter([self.message] if self.message else [])

    def delete_message(self, message):
        self.deleted.append(message)

    def send_message(self, content):
        self.sent.append(content)


class Lease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True

    def renew(self):
        pass


class Store:
    def __init__(self, status="queued", attempts=1):
        job_id = str(uuid4())
        self.job = {"id": job_id, "status": status, "required_text": ["Offer"], "size_bytes": 4}
        self.queue = Queue(SimpleNamespace(content=job_id, dequeue_count=attempts))
        self.poison = Queue()
        self.lease = Lease()
        self.reports = []
        self.credential = None

    def record_blob(self, job_id):
        return SimpleNamespace(acquire_lease=lambda **kwargs: self.lease)

    def read(self, job_id, lease=None):
        return dict(self.job)

    def save(self, job, lease=None):
        self.job = dict(job)

    def report(self, job_id, report):
        self.reports.append(report)

    def download(self, job, target):
        target.write_bytes(b"test")


def analysis(path, required_text):
    assert path.read_bytes() == b"test"
    return evaluate(
        {"width": 720, "height": 1280, "duration_seconds": 4, "has_audio": True},
        [], required_text,
    ), []


def test_cloud_worker_persists_conservative_report_and_acks(monkeypatch):
    monkeypatch.setenv("ENABLE_PAID_AI", "false")
    store = Store()
    assert process_one(store, analysis)
    assert store.job["status"] == "completed"
    assert len(store.queue.deleted) == 1
    assert store.reports[0]["ready"] is False
    assert store.reports[0]["coverage"]["ai_verified"] is False
    assert store.lease.released


def test_cloud_worker_deduplicates_completed_job():
    store = Store(status="completed")
    assert process_one(store, lambda *args: pytest.fail("Completed job reanalyzed"))
    assert store.queue.deleted and not store.reports


def test_cloud_worker_poison_after_retry_limit():
    store = Store(attempts=4)
    assert process_one(store, lambda *args: pytest.fail("Poison job reanalyzed"))
    assert store.job["status"] == "failed"
    assert store.poison.sent == [store.job["id"]]
    assert not store.reports


def test_cloud_worker_invalid_media_persists_failure_evidence():
    def broken(*args):
        raise MediaError("invalid-video-metadata")
    store = Store()
    assert process_one(store, broken)
    assert store.job["status"] == "failed"
    assert store.job["error"] == "invalid-video-metadata"
    assert store.reports[0]["error_code"] == "invalid-video-metadata"
    assert store.reports[0]["ready"] is False
    assert store.queue.deleted


def test_cloud_report_preserves_speech_and_provides_it_to_text_safety(monkeypatch):
    transcript = {"state": "needs-review", "text": "Recognized speech.", "segments": []}
    def with_transcript(path, required_text):
        report, frames = analysis(path, required_text)
        report["transcript"] = transcript
        return report, frames
    def collect(self, video_url, frames, required_text, *, recognized_transcript=None):
        assert recognized_transcript == "Recognized speech."
        return {"checks": [], "evidence": {}}
    monkeypatch.setattr("videoextration.azure_worker.AzureAI.collect", collect)
    store = Store()
    assert process_one(store, with_transcript)
    assert store.reports[0]["transcript"] == transcript
    assert store.reports[0]["ready"] is False
