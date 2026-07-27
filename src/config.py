"""Configuration, read from the environment.

Each service declares only the variables it actually needs, so the web app does
not refuse to start because the camera URL is unset.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigError(RuntimeError):
    """A required environment variable is missing or unusable."""


def _with_path(url: str, path: str) -> str:
    """Swap the path of an RTSP URL, keeping the credentials in one place.

    Cameras publish a smaller substream on a second path, and that substream is
    often the difference between an upload that keeps up with real time and one
    that does not — so selecting it must not mean duplicating the password.
    """
    path = path.strip()
    if not path:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _require(name: str, env: dict[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True)
class WebSettings:
    """What the FastAPI service needs. It never touches the camera or the bucket."""

    public_base_url: str
    stream_id: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WebSettings":
        env = os.environ if env is None else env
        return cls(
            public_base_url=_require("PUBLIC_BASE_URL", env).rstrip("/"),
            # The same STREAM_ID the uploader writes under. The page only suggests
            # it — any other path a visitor types is still resolved and tried.
            stream_id=env.get("STREAM_ID", "default"),
        )


@dataclass(frozen=True)
class TranscodeSettings:
    """Whether and how to re-encode.

    Read separately from `IngestSettings` because a camera handed in on the page
    is captured by the web service, which has no `RTSP_URL` of its own — and it
    needs these knobs for the same reason the ingest service does.
    """

    enabled: bool
    video_bitrate: str | None
    scale_height: int | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TranscodeSettings":
        env = os.environ if env is None else env
        # Setting either knob implies a transcode — copying cannot change bitrate.
        bitrate = env.get("VIDEO_BITRATE", "").strip() or None
        height = env.get("SCALE_HEIGHT", "").strip() or None
        explicit = env.get("TRANSCODE", "").strip().lower() in {"1", "true", "yes"}
        return cls(
            enabled=explicit or bool(bitrate) or bool(height),
            video_bitrate=bitrate,
            scale_height=int(height) if height else None,
        )


@dataclass(frozen=True)
class IngestSettings:
    rtsp_url: str
    spool_dir: Path
    hls_time: int
    transcode: bool
    video_bitrate: str | None
    scale_height: int | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "IngestSettings":
        env = os.environ if env is None else env
        encode = TranscodeSettings.from_env(env)
        return cls(
            rtsp_url=_with_path(_require("RTSP_URL", env), env.get("RTSP_PATH", "")),
            spool_dir=Path(env.get("SPOOL_DIR", "/var/spool/hls")),
            hls_time=int(env.get("HLS_TIME", "4")),
            transcode=encode.enabled,
            video_bitrate=encode.video_bitrate,
            scale_height=encode.scale_height,
        )


@dataclass(frozen=True)
class UploaderSettings:
    spool_dir: Path
    stream_id: str
    bucket: str
    endpoint_url: str | None
    access_key_id: str
    secret_access_key: str
    region: str
    upload_interval: float
    workers: int
    spool_retention: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "UploaderSettings":
        env = os.environ if env is None else env
        return cls(
            workers=int(env.get("UPLOAD_WORKERS", "8")),
            # 0 keeps every segment on disk forever, which is what the spool did
            # before this knob existed.
            spool_retention=float(env.get("SPOOL_RETENTION", "300")),
            spool_dir=Path(env.get("SPOOL_DIR", "/var/spool/hls")),
            stream_id=env.get("STREAM_ID", "default"),
            bucket=_require("S3_BUCKET", env),
            endpoint_url=env.get("S3_ENDPOINT_URL") or None,
            access_key_id=_require("S3_ACCESS_KEY_ID", env),
            secret_access_key=_require("S3_SECRET_ACCESS_KEY", env),
            region=env.get("S3_REGION", "auto"),
            upload_interval=float(env.get("UPLOAD_INTERVAL", "1.0")),
        )
