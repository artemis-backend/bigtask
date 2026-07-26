"""Task 1 — docs/tdd/hls-command-builder.md

Every flag asserted here was verified against the real camera on 2026-07-26.
The three that are easy to lose and produce a silently broken stream are
`-an` (D12), `-start_number` (A1) and `+genpts` — each has its own test.
"""

from pathlib import Path

import pytest

from ingest.command import build_ffmpeg_args, next_start_number, resume_start_number

RTSP = "rtsp://admin:pw@10.0.0.1:554/"


def args(**over):
    base = dict(
        rtsp_url=RTSP,
        out_dir=Path("/spool"),
        hls_time=4,
        transcode=False,
        resume=False,
    )
    base.update(over)
    return build_ffmpeg_args(**base)


def value_after(argv, flag):
    """The token following `flag`, or None. Catches a flag emitted without its value."""
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def hls_flags(argv):
    return set((value_after(argv, "-hls_flags") or "").split("+")) - {""}


# --- argv: transport, input, muxer ------------------------------------------


def test_forces_tcp_transport():
    argv = args()
    assert value_after(argv, "-rtsp_transport") == "tcp"


def test_input_url_is_passed():
    assert value_after(args(), "-i") == RTSP


def test_event_playlist_type():
    assert value_after(args(), "-hls_playlist_type") == "event"


def test_hls_time_is_passed_through():
    assert value_after(args(hls_time=6), "-hls_time") == "6"


def test_segment_filename_is_inside_out_dir():
    argv = args(out_dir=Path("/var/spool/hls"))
    target = value_after(argv, "-hls_segment_filename")
    assert target == "/var/spool/hls/seg_%06d.ts"


def test_playlist_path_is_inside_out_dir():
    argv = args(out_dir=Path("/var/spool/hls"))
    assert argv[-1] == "/var/spool/hls/index.m3u8"


# --- the flags that silently break playback if lost -------------------------


def test_audio_is_always_dropped():
    """D12: camera AAC + `-c copy` video => MSE never starts. Verified in browser."""
    assert "-an" in args()
    assert "-an" in args(transcode=True)
    assert "-an" in args(resume=True, start_number=5)


def test_discardcorrupt_is_set():
    """The public-internet RTSP path delivers damaged packets; they must be dropped."""
    fflags = value_after(args(), "-fflags")
    assert fflags is not None
    assert "discardcorrupt" in set(fflags.split("+")) - {""}


def test_timestamps_come_from_arrival_time_not_genpts():
    """genpts stamps packets at the *declared* frame rate, and this camera lies.

    Measured: 601 frames labelled 30.0 s of media took 117 s of real time to
    arrive. Media time then advances slower than the wall clock and the live edge
    drifts away without bound, which is the one thing §2 forbids.
    """
    argv = args()
    assert value_after(argv, "-use_wallclock_as_timestamps") == "1"
    assert "genpts" not in (value_after(argv, "-fflags") or "")


def test_input_options_precede_the_input():
    """An option after -i applies to the output and silently does nothing here."""
    argv = args()
    assert argv.index("-use_wallclock_as_timestamps") < argv.index("-i")
    assert argv.index("-fflags") < argv.index("-i")


def test_program_date_time_always_present():
    """D5 latency measurement reads EXT-X-PROGRAM-DATE-TIME."""
    assert "program_date_time" in hls_flags(args())


def test_independent_segments_and_temp_file_always_present():
    flags = hls_flags(args())
    assert "independent_segments" in flags
    assert "temp_file" in flags


def test_delete_segments_never_appears():
    """It contradicts the EVENT/DVR goal (D6) under every combination."""
    for kw in [
        {},
        {"transcode": True},
        {"resume": True, "start_number": 3},
        {"transcode": True, "resume": True, "start_number": 9},
    ]:
        assert "delete_segments" not in hls_flags(args(**kw))


# --- resume / start_number (A1) ---------------------------------------------


def test_fresh_start_has_no_append_list():
    assert "append_list" not in hls_flags(args(resume=False))


def test_fresh_start_has_no_start_number():
    assert "-start_number" not in args(resume=False)


def test_resume_adds_append_list():
    assert "append_list" in hls_flags(args(resume=True))


def test_resume_alone_omits_start_number():
    """Measured: with the playlist present, an explicit -start_number leaves a gap."""
    assert "-start_number" not in args(resume=True, start_number=None)


def test_explicit_start_number_is_emitted_when_given():
    """The recovery case — playlist lost — where ffmpeg would otherwise restart at 0."""
    assert value_after(args(resume=True, start_number=42), "-start_number") == "42"


def test_zero_is_emitted_and_not_confused_with_none():
    assert value_after(args(resume=True, start_number=0), "-start_number") == "0"


def test_negative_start_number_is_rejected():
    with pytest.raises(ValueError):
        args(resume=True, start_number=-1)


# --- resume_start_number: when the flag is needed at all --------------------


def test_no_start_number_when_playlist_present(tmp_path):
    (tmp_path / "index.m3u8").touch()
    (tmp_path / "seg_000000.ts").touch()
    assert resume_start_number(tmp_path) is None


def test_explicit_start_number_when_playlist_lost(tmp_path):
    """Without the playlist ffmpeg restarts at 0 and overwrites the DVR history."""
    for n in (0, 1, 2):
        (tmp_path / f"seg_{n:06d}.ts").touch()
    assert resume_start_number(tmp_path) == 3


def test_empty_dir_yields_zero(tmp_path):
    assert resume_start_number(tmp_path) == 0


# --- codec branch (D8) ------------------------------------------------------


def test_copy_when_not_transcoding():
    argv = args(transcode=False)
    assert value_after(argv, "-c:v") == "copy"
    assert "libx264" not in argv


def test_libx264_when_transcoding():
    assert value_after(args(transcode=True), "-c:v") == "libx264"


# --- next_start_number ------------------------------------------------------


def test_empty_dir_starts_at_zero(tmp_path):
    assert next_start_number(tmp_path) == 0


def test_missing_dir_starts_at_zero(tmp_path):
    assert next_start_number(tmp_path / "nope") == 0


def test_continues_after_highest_index(tmp_path):
    for n in (0, 1, 2):
        (tmp_path / f"seg_{n:06d}.ts").touch()
    assert next_start_number(tmp_path) == 3


def test_gaps_do_not_lower_the_result(tmp_path):
    (tmp_path / "seg_000000.ts").touch()
    (tmp_path / "seg_000007.ts").touch()
    assert next_start_number(tmp_path) == 8


def test_ignores_playlist_and_tmp_and_junk(tmp_path):
    (tmp_path / "seg_000003.ts").touch()
    (tmp_path / "index.m3u8").touch()
    (tmp_path / "seg_000009.ts.tmp").touch()  # temp_file in flight — not a real segment
    (tmp_path / "seg_00009.ts").touch()  # five digits, not our pattern
    (tmp_path / "segment_000012.ts").touch()
    (tmp_path / "notes.txt").touch()
    assert next_start_number(tmp_path) == 4


def test_recovery_path_composes_end_to_end(tmp_path):
    """Playlist lost, segments survive: the pieces must line up without a caller fixup."""
    (tmp_path / "seg_000004.ts").touch()
    argv = args(out_dir=tmp_path, resume=True, start_number=resume_start_number(tmp_path))
    assert value_after(argv, "-start_number") == "5"
    assert "append_list" in hls_flags(argv)


def test_normal_restart_composes_end_to_end(tmp_path):
    """Playlist intact: ffmpeg is left to continue the numbering on its own."""
    (tmp_path / "index.m3u8").touch()
    (tmp_path / "seg_000004.ts").touch()
    argv = args(out_dir=tmp_path, resume=True, start_number=resume_start_number(tmp_path))
    assert "-start_number" not in argv
    assert "append_list" in hls_flags(argv)
