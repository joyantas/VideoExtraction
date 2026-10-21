"""Environment configuration; local identity is opt-in and never valid in Azure mode."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mode: str = "local"
    data_dir: Path = Path("data")
    local_bearer_token: str = ""
    local_owner: str = "local-user"
    allowed_origins: tuple[str, ...] = ()
    max_upload_bytes: int = 100 * 1024 * 1024
    upload_ttl_seconds: int = 600
    upload_wall_seconds: int = 120
    max_duration_seconds: float = 120.0
    max_dimension: int = 4096
    max_pixels: int = 4096 * 2160
    max_fps: float = 120.0
    max_sample_frames: int = 6
    media_wall_seconds: int = 45
    lease_seconds: int = 180
    poll_seconds: float = 2.0
    ffprobe_binary: str = "ffprobe"
    ffmpeg_binary: str = "ffmpeg"

    def __post_init__(self):
        if self.mode not in {"local", "azure"}:
            raise ValueError("MODE must be local or azure")
        if self.mode == "azure" and self.local_bearer_token:
            raise ValueError("LOCAL_BEARER_TOKEN is forbidden when MODE=azure")
        if self.local_bearer_token and len(self.local_bearer_token) < 32:
            raise ValueError("LOCAL_BEARER_TOKEN must contain at least 32 characters")
        if not self.local_owner.strip():
            raise ValueError("LOCAL_OWNER must not be empty")
        if not (1 <= self.max_sample_frames <= 12):
            raise ValueError("MAX_SAMPLE_FRAMES must be between 1 and 12")
        if not (1 <= self.max_upload_bytes <= 1024 * 1024 * 1024):
            raise ValueError("MAX_UPLOAD_BYTES must be between 1 byte and 1 GiB")
        if not (1 <= self.max_duration_seconds <= 600):
            raise ValueError("MAX_DURATION_SECONDS must be between 1 and 600")
        if not (1 <= self.media_wall_seconds <= 120):
            raise ValueError("MEDIA_WALL_SECONDS must be between 1 and 120")
        if self.lease_seconds <= self.media_wall_seconds + 30:
            raise ValueError("LEASE_SECONDS must exceed the media budget by 30 seconds")
        if self.upload_ttl_seconds <= 0 or self.upload_wall_seconds <= 0:
            raise ValueError("Upload time limits must be positive")
        if not (1 <= self.max_dimension <= 8192 and 1 <= self.max_pixels <= 8192**2):
            raise ValueError("Invalid frame dimension limits")
        if not (1 <= self.max_fps <= 240) or self.poll_seconds <= 0:
            raise ValueError("Invalid frame rate or polling interval")

    @classmethod
    def from_env(cls):
        return cls(
            mode=os.getenv("MODE", "local"),
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            local_bearer_token=os.getenv("LOCAL_BEARER_TOKEN", ""),
            local_owner=os.getenv("LOCAL_OWNER", "local-user"),
            allowed_origins=tuple(
                value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",")
                if value.strip()
            ),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))),
            upload_ttl_seconds=int(os.getenv("UPLOAD_TTL_SECONDS", "600")),
            upload_wall_seconds=int(os.getenv("UPLOAD_WALL_SECONDS", "120")),
            max_duration_seconds=float(os.getenv("MAX_DURATION_SECONDS", "120")),
            max_dimension=int(os.getenv("MAX_DIMENSION", "4096")),
            max_pixels=int(os.getenv("MAX_PIXELS", str(4096 * 2160))),
            max_fps=float(os.getenv("MAX_FPS", "120")),
            max_sample_frames=int(os.getenv("MAX_SAMPLE_FRAMES", "6")),
            media_wall_seconds=int(os.getenv("MEDIA_WALL_SECONDS", "45")),
            lease_seconds=int(os.getenv("LEASE_SECONDS", "180")),
            poll_seconds=float(os.getenv("POLL_SECONDS", "2")),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe"),
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
        )
