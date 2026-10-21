"""Run with python -m videoextration.worker [--once]. Leases survive worker crashes."""

import argparse
import hashlib
import logging
import signal
import sqlite3
import time
from pathlib import Path

from .config import Settings
from .media import LocalMediaAnalyzer, MediaError
from .rules import SEMANTIC_CHECKS, evaluate
from .store import load_backend
from .transcription import transcribe_video


log = logging.getLogger(__name__)


def analyze_video(path, required_text, settings=None):
    """Return (report, [(requested_timestamp_seconds, Path), ...]) for local/Azure workers."""
    settings = settings or Settings.from_env()
    result = LocalMediaAnalyzer(settings).analyze(Path(path))
    report = evaluate(result["metadata"], result["frames"], required_text)
    report["transcript"] = transcribe_video(path, result["metadata"], settings)
    frames = [
        (frame["requested_timestamp_seconds"], Path(frame_path))
        for frame, frame_path in zip(result["frames"], result["sampled_frame_paths"], strict=True)
    ]
    return report, frames


def _conservative_summary(report):
    states = {item["state"] for item in report["findings"]}
    if not states.issubset({"pass", "fail", "needs-review", "not-evaluated"}):
        raise ValueError("Unknown finding state")
    expected_rules = {
        "portrait-aspect", "minimum-resolution", "duration", "audio-present",
        "rotation", "sampled-dark-frames",
    } | {rule_id for rule_id, _ in SEMANTIC_CHECKS}
    found_rules = {item["rule_id"] for item in report["findings"]}
    coverage = report.get("coverage", {})
    complete = (
        expected_rules.issubset(found_rules) and "not-evaluated" not in states
        and coverage.get("ai_verified") is True
        and coverage.get("ocr_configured") is True
        and not report.get("error_code")
    )
    report["complete"] = complete
    report["overall_state"] = (
        "fail" if "fail" in states else
        "needs-review" if "needs-review" in states or not complete else "pass"
    )
    if "not-evaluated" in states and not ({"fail", "needs-review"} & states):
        report["overall_state"] = "not-evaluated"
    report["ready"] = complete and states == {"pass"}
    return report


def process_once(settings=None, *, backend=None, analyzer=None, enrich=None):
    """Enrich(job, report, sampled_frame_paths)->report, then persist with a lease fence."""
    settings = settings or Settings.from_env()
    backend = backend or load_backend(settings)
    store, blobs = backend.store, backend.blobs
    job = store.claim(settings.lease_seconds)
    if job is None:
        return False
    analyzer = analyzer or LocalMediaAnalyzer(settings)
    enrich = enrich or backend.enrich
    required_text = job["request"]["required_text"]
    result = None
    try:
        if job.get("attempts", 1) > 3:
            raise MediaError("retry-limit")
        path = blobs.materialize(job["id"])
        size, digest = 0, hashlib.sha256()
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise MediaError("media-size-limit")
                digest.update(chunk)
        if size != job["actual_size"] or digest.hexdigest() != job["sha256"]:
            raise MediaError("media-integrity-mismatch")
        result = analyzer.analyze(Path(path))
        report = evaluate(result["metadata"], result["frames"], required_text)
        report["transcript"] = transcribe_video(path, result["metadata"], settings)
        if enrich is not None:
            report = enrich(job, report, result.get("sampled_frame_paths", []))
        report = _conservative_summary(report)
        report["job_id"] = job["id"]
        report["source_sha256"] = job["sha256"]
        if not store.complete(job["id"], job["lease_id"], report):
            log.warning("Lease lost before completion: %s", job["id"])
    except (MediaError, RuntimeError, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, MediaError) else "processing-provider-error"
        log.warning("Processing failed: job=%s code=%s", job["id"], code)
        report = evaluate(
            result["metadata"] if result else None, result["frames"] if result else [],
            required_text, error_code=code,
        )
        report["job_id"], report["source_sha256"] = job["id"], job.get("sha256")
        if not store.fail(job["id"], job["lease_id"], report):
            log.warning("Lease lost before failure persistence: %s", job["id"])
    return True


def main():
    settings = Settings.from_env()
    if settings.mode == "azure":
        from .azure_worker import main as azure_main
        return azure_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one queued job, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend = load_backend(settings)
    if args.once:
        process_once(settings, backend=backend)
        return
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            worked = process_once(settings, backend=backend)
        except (sqlite3.Error, OSError) as exc:
            log.error("Local queue unavailable: %s", type(exc).__name__)
            worked = False
        if not worked:
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
