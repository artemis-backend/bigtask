"""Run one ffmpeg capture and exit with its status.

Restarting is the container runtime's job (`restart: unless-stopped`), not this
process's: ffmpeg has no RTSP reconnect of its own, and a supervisor written here
would duplicate what Docker already does correctly, including backoff.
"""

import logging
import subprocess
import sys

from config import IngestSettings
from ingest.command import build_ffmpeg_args, resume_start_number

logging.basicConfig(level=logging.INFO, format="%(asctime)s ingest %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    settings = IngestSettings.from_env()
    settings.spool_dir.mkdir(parents=True, exist_ok=True)

    start_number = resume_start_number(settings.spool_dir)
    argv = build_ffmpeg_args(
        rtsp_url=settings.rtsp_url,
        out_dir=settings.spool_dir,
        hls_time=settings.hls_time,
        transcode=settings.transcode,
        resume=True,
        start_number=start_number,
        video_bitrate=settings.video_bitrate,
        scale_height=settings.scale_height,
    )

    # The URL carries the camera password, so log the shape of the run, not argv.
    log.info(
        "starting: spool=%s hls_time=%s transcode=%s bitrate=%s start_number=%s",
        settings.spool_dir, settings.hls_time, settings.transcode,
        settings.video_bitrate or "-", start_number,
    )
    return subprocess.run(argv).returncode


if __name__ == "__main__":
    sys.exit(main())
