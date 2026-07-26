"""Task 5 — docs/tdd/segment-uploader.md

The load-bearing invariant is that a segment is uploaded only once ffmpeg has
listed it in the playlist. Everything else here guards the two silent failure
modes: a truncated segment nobody re-uploads, and a playlist that points at
objects the bucket does not have yet.
"""

import threading
import time

import pytest

from uploader.storage import CONTENT_TYPES, content_type_for
from uploader.sync import SyncState, load_state, save_state, segments_in_playlist, sync_once

PLAYLIST = """\
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-INDEPENDENT-SEGMENTS
#EXTINF:5.047333,
#EXT-X-PROGRAM-DATE-TIME:2026-07-26T15:27:50.825+0000
seg_000000.ts
#EXTINF:5.000000,
seg_000001.ts
"""


class FakeStorage:
    """Records every put in order; can be told to fail on specific keys."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def put(self, key, data, content_type):
        if key in self.fail_on:
            raise OSError(f"boom: {key}")
        self.calls.append((key, content_type, len(data)))

    @property
    def keys(self):
        return [key for key, _, _ in self.calls]


def spool_with(tmp_path, playlist=PLAYLIST, segments=("seg_000000.ts", "seg_000001.ts"), extra=()):
    (tmp_path / "index.m3u8").write_text(playlist)
    for name in segments:
        (tmp_path / name).write_bytes(b"\x47" + name.encode())
    for name in extra:
        (tmp_path / name).write_bytes(b"junk")
    return tmp_path


# --- playlist parsing -------------------------------------------------------


def test_parses_segment_names_only():
    assert segments_in_playlist(PLAYLIST) == ["seg_000000.ts", "seg_000001.ts"]


def test_playlist_without_segments_is_empty():
    assert segments_in_playlist("#EXTM3U\n#EXT-X-VERSION:6\n") == []


def test_blank_lines_and_comments_are_ignored():
    text = "#EXTM3U\n\n# just a comment\n#EXTINF:4,\nseg_000000.ts\n\n"
    assert segments_in_playlist(text) == ["seg_000000.ts"]


def test_preserves_playlist_order():
    text = "#EXTINF:4,\nseg_000005.ts\n#EXTINF:4,\nseg_000002.ts\n"
    assert segments_in_playlist(text) == ["seg_000005.ts", "seg_000002.ts"]


# --- content types ----------------------------------------------------------


def test_content_types_are_explicit():
    """R2 infers the type when it is not set, and a wrong one breaks playback silently."""
    assert CONTENT_TYPES[".m3u8"] == "application/vnd.apple.mpegurl"
    assert CONTENT_TYPES[".ts"] == "video/mp2t"
    assert content_type_for("index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("seg_000000.ts") == "video/mp2t"


# --- the invariant ----------------------------------------------------------


def test_uploads_segments_listed_in_the_playlist(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert "s/demo/seg_000000.ts" in storage.keys
    assert "s/demo/seg_000001.ts" in storage.keys


def test_segment_on_disk_but_absent_from_playlist_is_not_uploaded(tmp_path):
    """ffmpeg lists a segment only after closing it — an unlisted file may be partial."""
    spool = spool_with(tmp_path, extra=("seg_000002.ts",))
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert "s/demo/seg_000002.ts" not in storage.keys


def test_tmp_files_are_never_uploaded(tmp_path):
    spool = spool_with(tmp_path, extra=("seg_000009.ts.tmp", "index.m3u8.tmp"))
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert not [k for k in storage.keys if k.endswith(".tmp")]


def test_playlist_is_uploaded_last(tmp_path):
    """Uploading it first would publish references to objects the bucket lacks."""
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert storage.keys[-1] == "s/demo/index.m3u8"
    assert len(storage.keys) == 3


def test_playlist_uses_its_own_content_type(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    key, ctype, _ = storage.calls[-1]
    assert (key, ctype) == ("s/demo/index.m3u8", "application/vnd.apple.mpegurl")


def test_segments_use_mpeg_ts_content_type(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert all(c == "video/mp2t" for k, c, _ in storage.calls if k.endswith(".ts"))


# --- idempotency ------------------------------------------------------------


def test_second_pass_uploads_no_segments(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    state = sync_once(spool, storage, "s/demo", SyncState())
    storage.calls.clear()
    sync_once(spool, storage, "s/demo", state)
    assert not [k for k in storage.keys if k.endswith(".ts")]


def test_unchanged_playlist_is_not_re_uploaded(tmp_path):
    """ffmpeg rewrites it once per segment; polling faster must not multiply writes."""
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    state = sync_once(spool, storage, "s/demo", SyncState())
    storage.calls.clear()
    sync_once(spool, storage, "s/demo", state)
    assert storage.calls == []


def test_new_segment_is_picked_up_on_the_next_pass(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    state = sync_once(spool, storage, "s/demo", SyncState())
    storage.calls.clear()

    (spool / "seg_000002.ts").write_bytes(b"\x47new")
    (spool / "index.m3u8").write_text(PLAYLIST + "#EXTINF:5.0,\nseg_000002.ts\n")

    sync_once(spool, storage, "s/demo", state)
    assert storage.keys == ["s/demo/seg_000002.ts", "s/demo/index.m3u8"]


# --- failure handling -------------------------------------------------------


def test_failed_segment_is_not_marked_uploaded(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage(fail_on={"s/demo/seg_000001.ts"})
    state = sync_once(spool, storage, "s/demo", SyncState())
    assert "s/demo/seg_000000.ts" in state.uploaded
    assert "s/demo/seg_000001.ts" not in state.uploaded


def test_playlist_is_withheld_while_a_segment_is_missing_from_the_bucket(tmp_path):
    """Publishing it now would point viewers at a 404."""
    spool = spool_with(tmp_path)
    storage = FakeStorage(fail_on={"s/demo/seg_000001.ts"})
    sync_once(spool, storage, "s/demo", SyncState())
    assert "s/demo/index.m3u8" not in storage.keys


def test_failed_segment_is_retried_next_pass(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage(fail_on={"s/demo/seg_000001.ts"})
    state = sync_once(spool, storage, "s/demo", SyncState())

    storage.fail_on.clear()
    storage.calls.clear()
    sync_once(spool, storage, "s/demo", state)
    assert storage.keys == ["s/demo/seg_000001.ts", "s/demo/index.m3u8"]


def test_segment_listed_but_not_yet_on_disk_holds_the_playlist_back(tmp_path):
    spool = spool_with(tmp_path, segments=("seg_000000.ts",))
    storage = FakeStorage()
    sync_once(spool, storage, "s/demo", SyncState())
    assert storage.keys == ["s/demo/seg_000000.ts"]


def test_missing_playlist_is_not_an_error(tmp_path):
    storage = FakeStorage()
    state = sync_once(tmp_path, storage, "s/demo", SyncState())
    assert storage.calls == []
    assert state.uploaded == set()


# --- persistence ------------------------------------------------------------


def test_state_survives_a_restart(tmp_path):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    state = sync_once(spool, storage, "s/demo", SyncState())
    save_state(spool, state)

    reloaded = load_state(spool)
    storage.calls.clear()
    sync_once(spool, storage, "s/demo", reloaded)
    assert storage.calls == []


def test_load_state_on_a_fresh_spool_is_empty(tmp_path):
    state = load_state(tmp_path)
    assert state.uploaded == set()
    assert state.playlist_digest is None


def test_corrupt_state_file_does_not_crash_the_uploader(tmp_path):
    (tmp_path / ".uploaded.json").write_text("{ not json")
    state = load_state(tmp_path)
    assert state.uploaded == set()


# --- concurrency ------------------------------------------------------------


class SlowStorage:
    """Records peak concurrency, so a silent return to serial uploads is caught."""

    def __init__(self, delay=0.02, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.delay = delay
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def put(self, key, data, content_type):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            time.sleep(self.delay)
            if key in self.fail_on:
                raise OSError(f"boom: {key}")
            with self._lock:
                self.calls.append(key)
        finally:
            with self._lock:
                self._live -= 1

    @property
    def keys(self):
        return list(self.calls)


def big_spool(tmp_path, count=12):
    names = [f"seg_{i:06d}.ts" for i in range(count)]
    body = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
    for name in names:
        body += f"#EXTINF:4.0,\n{name}\n"
        (tmp_path / name).write_bytes(b"\x47" + name.encode())
    (tmp_path / "index.m3u8").write_text(body)
    return tmp_path, names


def test_segments_upload_concurrently(tmp_path):
    """The cost of a segment is a round trip; serial uploads leave the link idle."""
    spool, _ = big_spool(tmp_path)
    storage = SlowStorage()
    sync_once(spool, storage, "s/demo", SyncState(), workers=8)
    assert storage.peak > 1


def test_worker_count_is_respected(tmp_path):
    spool, _ = big_spool(tmp_path)
    storage = SlowStorage()
    sync_once(spool, storage, "s/demo", SyncState(), workers=3)
    assert storage.peak <= 3


def test_serial_upload_still_possible(tmp_path):
    spool, _ = big_spool(tmp_path)
    storage = SlowStorage()
    sync_once(spool, storage, "s/demo", SyncState(), workers=1)
    assert storage.peak == 1


def test_playlist_still_last_under_concurrency(tmp_path):
    """The ordering invariant must survive parallelism, not just serial uploads."""
    spool, names = big_spool(tmp_path)
    storage = SlowStorage()
    sync_once(spool, storage, "s/demo", SyncState(), workers=8)
    assert storage.keys[-1] == "s/demo/index.m3u8"
    assert len(storage.keys) == len(names) + 1


def test_one_failure_among_many_still_withholds_the_playlist(tmp_path):
    spool, _ = big_spool(tmp_path)
    storage = SlowStorage(fail_on={"s/demo/seg_000007.ts"})
    state = sync_once(spool, storage, "s/demo", SyncState(), workers=8)
    assert "s/demo/index.m3u8" not in storage.keys
    assert "s/demo/seg_000007.ts" not in state.uploaded


def test_no_segment_is_uploaded_twice(tmp_path):
    spool, _ = big_spool(tmp_path)
    storage = SlowStorage()
    state = sync_once(spool, storage, "s/demo", SyncState(), workers=8)
    sync_once(spool, storage, "s/demo", state, workers=8)
    segments = [k for k in storage.keys if k.endswith(".ts")]
    assert len(segments) == len(set(segments))


@pytest.mark.parametrize("prefix", ["s/demo", "s/demo/", "/s/demo"])
def test_prefix_normalisation(tmp_path, prefix):
    spool = spool_with(tmp_path)
    storage = FakeStorage()
    sync_once(spool, storage, prefix, SyncState())
    assert storage.keys[-1] == "s/demo/index.m3u8"
