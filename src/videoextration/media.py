"""Bounded FFprobe/FFmpeg extraction. Input is a local opaque file, never a URL."""

import hashlib
import json
import math
import os
import selectors
import signal
import struct
import subprocess
import time
import zlib
from fractions import Fraction
from pathlib import Path

from .config import Settings


class MediaError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def bounded_command(args: list[str], deadline: float, output_limit: int) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaError("media-time-limit")
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise MediaError("media-tool-unavailable") from exc
    output = bytearray()
    stderr_size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError("media-time-limit")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        output.extend(chunk)
                        if len(output) > output_limit:
                            raise MediaError("media-output-limit")
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > 65536:
                            raise MediaError("media-diagnostic-limit")
        try:
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise MediaError("media-time-limit") from exc
        if returncode:
            raise MediaError("invalid-or-unsupported-media")
        return bytes(output)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _png(rgb: bytes, width: int, height: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    stride = width * 3
    scanlines = b"".join(b"\0" + rgb[start:start + stride] for start in range(0, len(rgb), stride))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class LocalMediaAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def analyze(self, path: Path) -> dict:
        settings = self.settings
        if path.is_symlink() or not path.is_file():
            raise MediaError("media-file-missing")
        path = path.resolve()
        if not 0 < path.stat().st_size <= settings.max_upload_bytes:
            raise MediaError("media-size-limit")
        deadline = time.monotonic() + settings.media_wall_seconds
        # A demuxer allowlist excludes playlists/concat; file-only protocol blocks network access.
        input_options = [
            "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi",
            "-probesize", "5000000", "-analyzeduration", "5000000", "-max_alloc", "33554432",
        ]
        raw = bounded_command([
            settings.ffprobe_binary, "-v", "error", *input_options,
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,width,height,avg_frame_rate,duration:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(path),
        ], deadline, 1024 * 1024)
        try:
            probed = json.loads(raw)
            streams = probed["streams"]
            if len(streams) > 16:
                raise MediaError("media-stream-limit")
            video = next(stream for stream in streams if stream.get("codec_type") == "video")
            width, height = int(video["width"]), int(video["height"])
            duration = float(probed.get("format", {}).get("duration") or video.get("duration", 0))
            fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
            rotation = float(video.get("tags", {}).get("rotate", 0))
            for side_data in video.get("side_data_list", []):
                if "rotation" in side_data:
                    rotation = float(side_data["rotation"])
            if not all(math.isfinite(value) for value in (duration, fps, rotation)):
                raise ValueError("Nonfinite metadata")
        except (KeyError, ValueError, TypeError, ZeroDivisionError, StopIteration) as exc:
            raise MediaError("invalid-video-metadata") from exc
        if width <= 0 or height <= 0 or max(width, height) > settings.max_dimension:
            raise MediaError("media-dimension-limit")
        if width * height > settings.max_pixels:
            raise MediaError("media-pixel-limit")
        if not 0 < duration <= settings.max_duration_seconds:
            raise MediaError("media-duration-limit")
        if not 0 < fps <= settings.max_fps:
            raise MediaError("media-frame-rate-limit")
        metadata = {
            "width": width, "height": height, "duration_seconds": duration,
            "fps": round(fps, 3), "rotation_degrees": rotation,
            "codec": video.get("codec_name", "unknown"),
            "container": probed.get("format", {}).get("format_name", "unknown"),
            "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
            "size_bytes": path.stat().st_size,
        }
        frame_dir = path.parent / (path.stem + "-frames")
        frame_dir.mkdir(mode=0o700, exist_ok=True)
        frames, frame_paths = [], []
        for index in range(settings.max_sample_frames):
            timestamp = duration * (index + 0.5) / settings.max_sample_frames
            rgb = bounded_command([
                settings.ffmpeg_binary, "-nostdin", "-v", "error", "-threads", "1",
                "-filter_threads", "1", "-filter_complex_threads", "1",
                *input_options, "-ss", f"{timestamp:.6f}", "-noautorotate", "-i", str(path),
                "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
                "-vf", "scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2,format=rgb24",
                "-threads", "1", "-f", "rawvideo", "pipe:1",
            ], deadline, 360 * 640 * 3)
            if len(rgb) != 360 * 640 * 3:
                raise MediaError("frame-decode-incomplete")
            # Exclude letterboxing from the observation by inspecting the scaled content rectangle.
            factor = min(360 / width, 640 / height)
            content_width, content_height = max(1, int(width * factor)), max(1, int(height * factor))
            left, top = (360 - content_width) // 2, (640 - content_height) // 2
            luminances = [
                (77 * rgb[p] + 150 * rgb[p + 1] + 29 * rgb[p + 2]) >> 8
                for y in range(top, top + content_height)
                for x in range(left, left + content_width)
                for p in ((y * 360 + x) * 3,)
            ]
            png = _png(rgb, 360, 640)
            frame_path = frame_dir / f"frame-{index:02d}.png"
            frame_path.write_bytes(png)
            frame_paths.append(str(frame_path))
            frames.append({
                "index": index, "requested_timestamp_seconds": round(timestamp, 4),
                "mean_luminance": round(sum(luminances) / len(luminances), 3),
                "dark_pixel_fraction": round(sum(value < 16 for value in luminances) / len(luminances), 5),
                "sha256": hashlib.sha256(png).hexdigest(),
                "sample_size": {"width": 360, "height": 640},
            })
            if time.monotonic() > deadline:
                raise MediaError("media-time-limit")
        return {"metadata": metadata, "frames": frames, "sampled_frame_paths": frame_paths}
