"""Start capture and upload for a camera handed in at runtime.

The compose `ingest` service covers the camera configured in `.env`. This covers
the other reading of the spec — a page you hand a camera to — by running the same
two jobs in-process: ffmpeg into a per-stream spool, and the uploader out of it.

The stream id is derived from the URL, so asking for the same camera twice
returns the same broadcast instead of starting a second copy of it.
"""

import hashlib
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import TranscodeSettings, UploaderSettings
from ingest.command import build_ffmpeg_args, resume_start_number
from uploader.storage import S3ObjectStorage
from uploader.sync import run_forever

RTSP_SCHEMES = frozenset({"rtsp", "rtsps"})
ID_PREFIX = "cam-"
_RESTART_DELAY_S = 5.0

log = logging.getLogger(__name__)


class InvalidCamera(ValueError):
    """The submitted camera URL is unusable."""


def is_camera_url(src: str) -> bool:
    return urlsplit(src.strip()).scheme.lower() in RTSP_SCHEMES


def stream_id_for(rtsp_url: str) -> str:
    """Stable, opaque id. Hashing the whole URL keeps credentials out of it."""
    digest = hashlib.sha256(rtsp_url.strip().encode()).hexdigest()[:12]
    return f"{ID_PREFIX}{digest}"


def _redacted(rtsp_url: str) -> str:
    """Host and path only — safe to log and to show on a page."""
    parts = urlsplit(rtsp_url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))


def validate_camera_url(src: str) -> str:
    src = (src or "").strip()
    parts = urlsplit(src)
    if parts.scheme.lower() not in RTSP_SCHEMES:
        raise InvalidCamera("адрес камеры должен начинаться с rtsp:// или rtsps://")
    if not parts.hostname:
        raise InvalidCamera("в адресе камеры нет хоста")
    return src


@dataclass
class Stream:
    stream_id: str
    source: str          # redacted; the credentialled URL is never stored here
    spool: Path


class StreamManager:
    """Runs one ffmpeg and one uploader per requested camera."""

    def __init__(
        self,
        settings: UploaderSettings,
        spool_root: Path,
        hls_time: int = 4,
        transcode: TranscodeSettings | None = None,
    ):
        self._settings = settings
        self._spool_root = Path(spool_root)
        self._hls_time = hls_time
        self._transcode = transcode or TranscodeSettings(False, None, None)
        self._streams: dict[str, Stream] = {}
        self._lock = threading.Lock()

    def list_streams(self) -> list[Stream]:
        with self._lock:
            return list(self._streams.values())

    def start(self, rtsp_url: str) -> Stream:
        rtsp_url = validate_camera_url(rtsp_url)
        stream_id = stream_id_for(rtsp_url)

        with self._lock:
            existing = self._streams.get(stream_id)
            if existing is not None:
                return existing

            spool = self._spool_root / stream_id
            spool.mkdir(parents=True, exist_ok=True)
            stream = Stream(stream_id=stream_id, source=_redacted(rtsp_url), spool=spool)
            self._streams[stream_id] = stream

        # Daemon threads: the process exits without waiting for a camera to stop.
        threading.Thread(
            target=self._capture_forever, args=(rtsp_url, spool),
            name=f"ingest-{stream_id}", daemon=True,
        ).start()
        threading.Thread(
            target=self._upload_forever, args=(stream_id, spool),
            name=f"upload-{stream_id}", daemon=True,
        ).start()

        log.info("started stream %s for %s", stream_id, stream.source)
        return stream

    def _capture_forever(self, rtsp_url: str, spool: Path) -> None:
        """ffmpeg has no RTSP reconnect; restarting it is what stands in for one."""
        while True:
            argv = build_ffmpeg_args(
                rtsp_url=rtsp_url,
                out_dir=spool,
                hls_time=self._hls_time,
                transcode=self._transcode.enabled,
                resume=True,
                start_number=resume_start_number(spool),
                video_bitrate=self._transcode.video_bitrate,
                scale_height=self._transcode.scale_height,
            )
            try:
                code = subprocess.run(argv).returncode
            except Exception:
                log.exception("ffmpeg failed to launch for %s", spool.name)
                code = -1
            log.warning("ffmpeg for %s exited with %s, restarting", spool.name, code)
            threading.Event().wait(_RESTART_DELAY_S)

    def _upload_forever(self, stream_id: str, spool: Path) -> None:
        storage = S3ObjectStorage(
            bucket=self._settings.bucket,
            access_key_id=self._settings.access_key_id,
            secret_access_key=self._settings.secret_access_key,
            endpoint_url=self._settings.endpoint_url,
            region=self._settings.region,
            max_pool_connections=self._settings.workers * 2,
        )
        run_forever(spool, storage, stream_id,
                    self._settings.upload_interval, self._settings.workers,
                    self._settings.spool_retention)
