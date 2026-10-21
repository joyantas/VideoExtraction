from videoextration.media import MediaError
from videoextration.worker import _conservative_summary, process_once


class FakeAnalyzer:
    def analyze(self, path):
        return {
            "metadata": {"width": 1080, "height": 1920, "duration_seconds": 10, "has_audio": True},
            "frames": [{"index": 0, "requested_timestamp_seconds": 5, "dark_pixel_fraction": 0.1}],
            "sampled_frame_paths": [],
        }


def test_worker_persists_report_and_owner_scopes_it(settings, backend, queued_job, client, auth):
    assert process_once(settings, backend=backend, analyzer=FakeAnalyzer())
    stored = backend.store.get(queued_job["id"])
    assert stored["state"] == "completed"
    report = client.get(f"/api/jobs/{queued_job['id']}/report", headers=auth).json()
    assert report["ready"] is False and report["complete"] is False
    assert report["source_sha256"] == stored["sha256"]
    assert report["generated_at"].endswith("Z")
    assert report["transcript"]["text"] is None
    assert report["transcript"]["state"] == "not-evaluated"
    assert client.get(
        f"/api/jobs/{queued_job['id']}/report",
        headers={"Authorization": "Bearer " + "b" * 40},
    ).status_code == 404
    assert not process_once(settings, backend=backend, analyzer=FakeAnalyzer())


def test_analyzer_error_produces_persisted_incomplete_report(settings, backend, queued_job):
    class BrokenAnalyzer:
        def analyze(self, path):
            raise MediaError("invalid-or-unsupported-media")
    assert process_once(settings, backend=backend, analyzer=BrokenAnalyzer())
    job = backend.store.get(queued_job["id"])
    assert job["state"] == "failed"
    assert job["report"]["error_code"] == "invalid-or-unsupported-media"
    assert job["report"]["ready"] is False


def test_integrity_mismatch_never_analyzed(settings, backend, queued_job):
    backend.blobs.path_for(queued_job["id"]).write_bytes(b"oops")
    assert process_once(settings, backend=backend, analyzer=FakeAnalyzer())
    assert backend.store.get(queued_job["id"])["report"]["error_code"] == "media-integrity-mismatch"


def test_enrichment_hook_cannot_override_unknown_findings_as_ready(settings, backend, queued_job):
    calls = []
    def enrich(job, report, paths):
        calls.append(job["id"])
        report["ready"] = True
        return report
    assert process_once(settings, backend=backend, analyzer=FakeAnalyzer(), enrich=enrich)
    assert calls == [queued_job["id"]]
    assert backend.store.get(queued_job["id"])["report"]["ready"] is False


def test_enrichment_error_cannot_leave_green_report(settings, backend, queued_job):
    def enrich(job, report, paths):
        raise RuntimeError("private provider details must not leak")
    assert process_once(settings, backend=backend, analyzer=FakeAnalyzer(), enrich=enrich)
    report = backend.store.get(queued_job["id"])["report"]
    assert report["error_code"] == "processing-provider-error"
    assert "private" not in str(report)
    assert report["ready"] is False


def test_provider_verification_is_required_for_readiness():
    report = {
        "findings": [{"rule_id": "portrait-aspect", "state": "pass"}],
        "coverage": {"ai_verified": False, "ocr_configured": True},
    }
    assert _conservative_summary(report)["ready"] is False


def test_recognized_audio_text_reaches_persisted_output_json(settings, backend, queued_job, client, auth, monkeypatch):
    transcript = {
        "state": "needs-review", "text": "A recognized campaign message.",
        "segments": [{"start_seconds": 0, "end_seconds": 2, "text": "A recognized campaign message."}],
        "source": "azure-speech", "human_verified": False,
    }
    monkeypatch.setattr("videoextration.worker.transcribe_video", lambda *args: transcript)
    assert process_once(settings, backend=backend, analyzer=FakeAnalyzer())
    report = client.get(f"/api/jobs/{queued_job['id']}/report", headers=auth).json()
    assert report["transcript"] == transcript
    assert report["ready"] is False
