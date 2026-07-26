"""Build the ffmpeg argv that turns an RTSP source into a growing HLS EVENT playlist.

See docs/tdd/hls-command-builder.md for why the flags are what they are.
"""

import re
from pathlib import Path

SEGMENT_TEMPLATE = "seg_%06d.ts"
PLAYLIST_NAME = "index.m3u8"

_SEGMENT_RE = re.compile(r"seg_(\d{6})\.ts\Z")

# discardcorrupt drops the damaged packets the lossy public-internet RTSP path
# produces. genpts is deliberately NOT here: it stamps arriving packets at the
# stream's *declared* frame rate, so a camera that delivers fewer frames than it
# advertises produces media time that advances slower than the wall clock, and
# the live edge falls behind without bound. Measured on this camera: 601 frames
# labelled 30.0 s of media arrived over 117 s of real time.
_INPUT_FFLAGS = ("discardcorrupt",)

# Timestamps come from arrival time instead. This both keeps the live edge pinned
# to now and supplies the initial PTS whose absence made the muxer abort with
# "first pts and dts value must be set".
_WALLCLOCK_TIMESTAMPS = ("-use_wallclock_as_timestamps", "1")

# delete_segments is deliberately absent: it expires segments, which is the
# opposite of what an EVENT playlist is for.
_BASE_HLS_FLAGS = ("program_date_time", "independent_segments", "temp_file")


def _double(bitrate: str) -> str:
    """`2500k` -> `5000k`. ffmpeg accepts a bare number or a k/M suffix."""
    match = re.fullmatch(r"(\d+)([kKmM]?)", bitrate.strip())
    if not match:
        raise ValueError(f"bitrate must look like '2500k', got {bitrate!r}")
    return f"{int(match.group(1)) * 2}{match.group(2)}"


def next_start_number(out_dir: Path) -> int:
    """One past the highest `seg_NNNNNN.ts` in out_dir; 0 if there are none."""
    try:
        names = [entry.name for entry in Path(out_dir).iterdir()]
    except (FileNotFoundError, NotADirectoryError):
        return 0

    indices = [int(m.group(1)) for name in names if (m := _SEGMENT_RE.match(name))]
    return max(indices) + 1 if indices else 0


def resume_start_number(out_dir: Path) -> int | None:
    """The `-start_number` to pass on restart, or None to let ffmpeg work it out.

    Measured behaviour of `append_list` (ffmpeg 8.1.2), both directions verified:

    - playlist present: ffmpeg continues numbering by itself. Passing an explicit
      `-start_number` *adds* to its own offset and leaves a gap in the sequence.
    - playlist absent (lost, or wiped between runs): ffmpeg restarts at 0 and
      overwrites the existing segments, destroying the DVR history.

    So the flag is neither always-right nor always-wrong; it is exactly the
    recovery case that needs it.
    """
    out_dir = Path(out_dir)
    if (out_dir / PLAYLIST_NAME).exists():
        return None
    return next_start_number(out_dir)


def build_ffmpeg_args(
    *,
    rtsp_url: str,
    out_dir: Path,
    hls_time: int,
    transcode: bool,
    resume: bool,
    start_number: int | None = None,
    video_bitrate: str | None = None,
    scale_height: int | None = None,
) -> list[str]:
    """argv for one ffmpeg run. On restart pass `start_number=resume_start_number(out_dir)`.

    Audio is always dropped: the camera's AAC track combined with a copied video
    stream leaves MSE unable to start playback at all (verified in-browser).

    `video_bitrate` and `scale_height` apply only when transcoding, and exist for
    one reason: the uplink to object storage can be narrower than the camera's
    output, and a copied stream that cannot be uploaded in real time falls behind
    without bound.
    """
    if start_number is not None and start_number < 0:
        raise ValueError(f"start_number must be >= 0, got {start_number}")
    if not transcode and (video_bitrate or scale_height):
        raise ValueError("video_bitrate and scale_height require transcode=True")

    out_dir = Path(out_dir)
    hls_flags = list(_BASE_HLS_FLAGS)
    if resume:
        hls_flags.append("append_list")

    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        *_WALLCLOCK_TIMESTAMPS,
        "-fflags", "+" + "+".join(_INPUT_FFLAGS),
        "-i", rtsp_url,
        "-an",
        "-c:v", "libx264" if transcode else "copy",
    ]
    if transcode:
        argv += ["-preset", "veryfast", "-g", str(hls_time * 10)]
        if scale_height:
            argv += ["-vf", f"scale=-2:{scale_height}"]
        if video_bitrate:
            # A hard cap, not an average: bufsize bounds how far a burst may
            # overshoot, which is what the uplink actually cares about.
            argv += ["-b:v", video_bitrate, "-maxrate", video_bitrate,
                     "-bufsize", f"{_double(video_bitrate)}"]

    argv += [
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_playlist_type", "event",
        "-hls_flags", "+".join(hls_flags),
        "-hls_segment_filename", str(out_dir / SEGMENT_TEMPLATE),
    ]
    if start_number is not None:
        argv += ["-start_number", str(start_number)]

    argv.append(str(out_dir / PLAYLIST_NAME))
    return argv
