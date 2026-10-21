import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from videoextration.media import LocalMediaAnalyzer, MediaError, bounded_command
from videoextration.worker import analyze_video


def probe_data(**video_changes):
    return {
        "format": {"duration": "10", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920,
             "avg_frame_rate": "30/1", "codec_name": "h264", **video_changes},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }


def mock_media(monkeypatch, workspace, probe=None):
    path = workspace / "opaque.media"
    path.write_bytes(b"test")
    calls = []
    def command(args, deadline, output_limit):
        calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps(probe if probe is not None else probe_data()).encode()
        return b"\x80\x80\x80" * (360 * 640)
    monkeypatch.setattr("videoextration.media.bounded_command", command)
    return path, calls


def test_sampled_frames_have_real_evidence_and_no_user_paths(settings, workspace, monkeypatch):
    path, calls = mock_media(monkeypatch, workspace)
    settings = replace(settings, max_sample_frames=2)
    result = LocalMediaAnalyzer(settings).analyze(path)
    assert result["metadata"]["width"] == 1080
    assert len(result["frames"]) == len(result["sampled_frame_paths"]) == 2
    assert result["frames"][0]["dark_pixel_fraction"] == 0
    assert result["frames"][0]["mean_luminance"] == 128
    for frame_path in result["sampled_frame_paths"]:
        from pathlib import Path
        assert Path(frame_path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert len(calls) == 3
    for args in calls:
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        assert args[args.index("-format_whitelist") + 1] == "mov,matroska,webm,avi"
    assert calls[1][calls[1].index("-frames:v") + 1] == "1"


@pytest.mark.parametrize("probe,code", [
    ({"streams": [], "format": {"duration": "10"}}, "invalid-video-metadata"),
    (probe_data(width=10000), "media-dimension-limit"),
    (probe_data(width=4096, height=4096), "media-pixel-limit"),
    (probe_data(avg_frame_rate="1000/1"), "media-frame-rate-limit"),
    (probe_data(avg_frame_rate="0/0"), "invalid-video-metadata"),
    ({**probe_data(), "format": {"duration": "nan"}}, "invalid-video-metadata"),
    ({**probe_data(), "format": {"duration": "500"}}, "media-duration-limit"),
])
def test_metadata_bounds_are_enforced_before_decode(settings, workspace, monkeypatch, probe, code):
    path, calls = mock_media(monkeypatch, workspace, probe)
    with pytest.raises(MediaError) as error:
        LocalMediaAnalyzer(settings).analyze(path)
    assert error.value.code == code
    assert len(calls) == 1


def test_reusable_analysis_contract(settings, workspace, monkeypatch):
    path, _ = mock_media(monkeypatch, workspace)
    report, frames = analyze_video(path, ["Exact user phrase"], replace(settings, max_sample_frames=1))
    assert report["required_text"] == ["Exact user phrase"]
    assert report["ready"] is False
    assert len(frames) == 1 and frames[0][0] == 5.0
    assert frames[0][1].is_file()


def test_process_stdout_limit():
    with pytest.raises(MediaError) as error:
        bounded_command([sys.executable, "-c", "print('x' * 4096)"], time.monotonic() + 3, 32)
    assert error.value.code == "media-output-limit"


def test_process_wall_time_limit():
    with pytest.raises(MediaError) as error:
        bounded_command([sys.executable, "-c", "import time; time.sleep(10)"], time.monotonic() + 0.05, 1024)
    assert error.value.code == "media-time-limit"


def test_missing_media_tool(workspace):
    with pytest.raises(MediaError) as error:
        bounded_command([str(workspace / "missing-tool")], time.monotonic() + 1, 1024)
    assert error.value.code == "media-tool-unavailable"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="System FFmpeg + FFprobe required")
def test_real_video_is_probed_and_decoded(workspace, settings):
    path = workspace / "generated.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=10:d=3",
        "-c:v", "mpeg4", "-y", str(path),
    ], check=True, timeout=10)
    report, frames = analyze_video(path, [], replace(settings, max_upload_bytes=1024 * 1024, max_sample_frames=2))
    metadata = next(item for item in report["evidence"] if item["id"] == "media-metadata")["observations"]
    assert metadata["width"] == 180 and metadata["duration_seconds"] == 3
    assert len(frames) == 2
    assert report["ready"] is False
