"""Real local upload -> durable queue -> separate worker process -> persisted report."""

import os
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from videoextration.api import create_app


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg + FFprobe required")
def test_real_campaign_upload_and_worker_process(settings, workspace):
    video = workspace / "campaign.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error",
        "-f", "lavfi", "-i", "color=c=blue:s=720x1280:r=25:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    ], check=True, timeout=30)
    data = video.read_bytes()
    settings = replace(settings, max_upload_bytes=100 * 1024 * 1024)
    client = TestClient(create_app(settings))
    headers = {"Authorization": f"Bearer {settings.local_bearer_token}"}
    job = client.post("/api/jobs", headers=headers, json={
        "filename": "../../never-used-as-path.mp4", "content_type": "video/mp4",
        "size_bytes": len(data), "required_text": ["Offer ends Friday"],
        "profile": "illustrative-portrait-v1",
    }).json()
    assert client.put(job["upload_url"], headers=job["upload_headers"], content=data).status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/complete", headers=headers).status_code == 202
    environment = {
        **os.environ, "MODE": "local", "DATA_DIR": str(settings.data_dir),
        "LOCAL_BEARER_TOKEN": settings.local_bearer_token, "MAX_SAMPLE_FRAMES": "3",
    }
    subprocess.run(
        [sys.executable, "-m", "videoextration.worker", "--once"],
        env=environment, check=True, timeout=60,
    )
    status = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert status["status"] == "completed"
    report = client.get(f"/api/jobs/{job['id']}/report", headers=headers).json()
    assert report["ready"] is False and report["complete"] is False
    assert report["coverage"]["sampled_frame_count"] == 3
    findings = {row["rule_id"]: row["state"] for row in report["findings"]}
    assert findings["portrait-aspect"] == findings["minimum-resolution"] == findings["duration"] == "pass"
    assert findings["frame-safety"] == findings["required-text"] == "not-evaluated"
    assert report["source_sha256"] == status["sha256"]
