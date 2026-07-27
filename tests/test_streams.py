"""Starting a camera handed in on the page.

The critical property here is that the camera's password never leaves the
process: it arrives in a URL, and everything derived from that URL — the stream
id that becomes a public bucket prefix, the source shown on the page — must be
free of it.
"""

import pytest

from config import TranscodeSettings
from web.streams import (
    InvalidCamera,
    _redacted,
    is_camera_url,
    stream_id_for,
    validate_camera_url,
)

CAMERA = "rtsp://admin:Sup3rSecret@10.0.0.5:554/Streaming/Channels/102"


# --- credentials must not escape --------------------------------------------


def test_stream_id_does_not_contain_credentials():
    """The id becomes a bucket prefix, and the bucket is world-readable."""
    assert "Sup3rSecret" not in stream_id_for(CAMERA)
    assert "admin" not in stream_id_for(CAMERA)


def test_redacted_source_drops_credentials():
    """This string is rendered on a page and written to logs."""
    redacted = _redacted(CAMERA)
    assert "Sup3rSecret" not in redacted
    assert "admin@" not in redacted
    assert "10.0.0.5:554" in redacted


# --- ids are stable and distinct --------------------------------------------


def test_same_camera_yields_the_same_id():
    """Otherwise asking twice starts a second recording of one camera."""
    assert stream_id_for(CAMERA) == stream_id_for(CAMERA)


def test_different_cameras_yield_different_ids():
    other = CAMERA.replace("10.0.0.5", "10.0.0.6")
    assert stream_id_for(CAMERA) != stream_id_for(other)


def test_different_credentials_yield_different_ids():
    """Same host, different account is a different request; do not merge them."""
    other = CAMERA.replace("Sup3rSecret", "other")
    assert stream_id_for(CAMERA) != stream_id_for(other)


def test_id_is_safe_as_a_url_path_segment():
    stream_id = stream_id_for(CAMERA)
    assert stream_id.replace("-", "").isalnum()
    assert "/" not in stream_id and ".." not in stream_id


# --- classification and validation ------------------------------------------


@pytest.mark.parametrize("src", [
    "rtsp://10.0.0.5:554/", "rtsps://10.0.0.5/", "RTSP://10.0.0.5/", "  rtsp://10.0.0.5/  ",
])
def test_camera_urls_are_recognised(src):
    assert is_camera_url(src)


@pytest.mark.parametrize("src", ["demo", "https://pub.example/demo/index.m3u8", ""])
def test_non_camera_sources_are_not_mistaken_for_cameras(src):
    assert not is_camera_url(src)


@pytest.mark.parametrize("src", ["", "demo", "http://example/x", "rtsp://", "file:///etc/passwd"])
def test_unusable_sources_are_rejected(src):
    with pytest.raises(InvalidCamera):
        validate_camera_url(src)


def test_valid_camera_url_is_returned_stripped():
    assert validate_camera_url(f"  {CAMERA}  ") == CAMERA


# --- transcode knobs reach the page-started capture too ----------------------
#
# A camera handed in on the page is captured by the web service, which has no
# RTSP_URL and therefore cannot read IngestSettings. Parsing these separately is
# what keeps VIDEO_BITRATE from silently applying to the compose service only.


def test_no_knobs_means_no_transcode():
    assert TranscodeSettings.from_env({}) == TranscodeSettings(False, None, None)


def test_bitrate_alone_enables_transcode():
    """Copying cannot change bitrate, so asking for one implies re-encoding."""
    encode = TranscodeSettings.from_env({"VIDEO_BITRATE": "2500k"})
    assert (encode.enabled, encode.video_bitrate) == (True, "2500k")


def test_scale_height_alone_enables_transcode():
    encode = TranscodeSettings.from_env({"SCALE_HEIGHT": "720"})
    assert (encode.enabled, encode.scale_height) == (True, 720)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_transcode_flag_is_accepted_in_the_usual_spellings(value):
    assert TranscodeSettings.from_env({"TRANSCODE": value}).enabled


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_transcode_flag_off(value):
    assert not TranscodeSettings.from_env({"TRANSCODE": value}).enabled


def test_blank_knobs_are_treated_as_unset():
    """docker-compose passes an empty string for a variable absent from .env."""
    encode = TranscodeSettings.from_env({"VIDEO_BITRATE": "  ", "SCALE_HEIGHT": ""})
    assert encode == TranscodeSettings(False, None, None)
