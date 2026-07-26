"""Probe the camera for a lower-bitrate substream.

The uplink is the binding constraint on the cloud path: if the camera already
publishes a second, smaller stream, ingest can keep `-c copy` instead of paying
for a transcode. Prints paths and measurements only — never the credentialled URL.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

IMAGE = os.environ.get("FFMPEG_IMAGE", "bigbro-stream:local")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MEASURE_SECONDS = 8

# Hikvision publishes the main stream on 101 and progressively smaller ones on
# 102/103; the /h264/ form is what older firmware answers to.
CANDIDATE_PATHS = [
    "",
    "/Streaming/Channels/101",
    "/Streaming/Channels/102",
    "/Streaming/Channels/103",
    "/h264/ch1/sub/av_stream",
]


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def with_path(url: str, path: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", args[0], IMAGE, *args[1:]],
        capture_output=True, text=True, timeout=timeout,
    )


def describe(url: str) -> dict | None:
    proc = run([
        "ffprobe", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", url,
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
        "-of", "json",
    ], timeout=40)
    if proc.returncode != 0:
        return {"error": (proc.stderr.strip().splitlines() or ["no output"])[-1][:120]}
    streams = json.loads(proc.stdout or "{}").get("streams") or []
    return streams[0] if streams else {"error": "no video stream"}


def measure_bitrate(url: str) -> float | None:
    """Mbit/s, measured by copying for a few seconds — RTSP rarely reports it."""
    proc = run([
        "ffmpeg", "-hide_banner", "-rtsp_transport", "tcp",
        "-i", url, "-t", str(MEASURE_SECONDS), "-an", "-c", "copy",
        "-f", "mpegts", "-y", "/dev/null",
    ], timeout=MEASURE_SECONDS + 40)
    match = re.search(r"video:(\d+)kB", proc.stderr)
    if not match:
        return None
    return int(match.group(1)) * 8 / MEASURE_SECONDS / 1000


def main() -> int:
    env = load_env(ENV_FILE)
    base = env.get("RTSP_URL", "").strip()
    if not base:
        print("RTSP_URL is not set in .env")
        return 1

    for path in CANDIDATE_PATHS:
        url = with_path(base, path)
        label = path or "(без пути)"
        info = describe(url)
        if info is None or "error" in info:
            print(f"{label:<32} — недоступен: {info.get('error') if info else 'timeout'}", flush=True)
            continue
        size = f"{info.get('width')}x{info.get('height')}"
        rate = measure_bitrate(url)
        rate_text = f"{rate:.1f} Мбит/с" if rate else "не измерен"
        print(f"{label:<32} {info.get('codec_name'):<6} {size:<12} {rate_text}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
