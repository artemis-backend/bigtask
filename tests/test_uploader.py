"""Task 5 — docs/tdd/segment-uploader.md

The load-bearing invariant is that a segment is uploaded only once ffmpeg has
listed it in the playlist. Everything else here guards the two silent failure
modes: a truncated segment nobody re-uploads, and a playlist that points at
objects the bucket does not have yet.
"""

import threading
import time

import os
import time

import pytest

from uploader.storage import CONTENT_TYPES, cache_control_for, content_type_for
from uploader.sync import (
    PLAYLIST_NAME,
    STATE_FILE,
    SyncState,
    load_state,
    prunable_segments,
    prune_spool,
    purge_prefix,
    save_state,
    segments_in_playlist,
    spool_is_fresh,
    sync_once,
)

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

    def __init__(self, fail_on=(), existing=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.stored = dict.fromkeys(existing, b"")
        self.deleted = []

    def put(self, key, data, content_type):
        if key in self.fail_on:
            raise OSError(f"boom: {key}")
        self.calls.append((key, content_type, len(data)))
        self.stored[key] = data

    def list_keys(self, prefix):
        return [key for key in self.stored if key.startswith(prefix)]

    def delete(self, keys):
        for key in keys:
            self.stored.pop(key, None)
            self.deleted.append(key)

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


def test_the_playlist_must_be_revalidated_every_time():
    """Served stale, the player never sees the stream grow past the cached copy."""
    assert cache_control_for("index.m3u8") == "no-cache"


def test_segments_are_not_cached_as_immutable():
    """A capture restarting from zero reuses seg_000000.ts for different video."""
    value = cache_control_for("seg_000000.ts")
    assert "immutable" not in value
    assert "max-age=60" in value


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


# --- pruning the spool -------------------------------------------------------
#
# This is the only code in the project that deletes anything, and what it deletes
# is the recording. Every guard below is a way the DVR history could be lost.


def aged_spool(tmp_path, count=5, age_s=3600):
    """A spool of `count` segments, all written `age_s` ago, all uploaded."""
    (tmp_path / PLAYLIST_NAME).write_text(
        "#EXTM3U\n" + "".join(f"#EXTINF:5,\nseg_{i:06d}.ts\n" for i in range(count))
    )
    old = time.time() - age_s
    for i in range(count):
        path = tmp_path / f"seg_{i:06d}.ts"
        path.write_bytes(b"\x47" * 16)
        os.utime(path, (old, old))
    state = SyncState(uploaded={f"demo/seg_{i:06d}.ts" for i in range(count)})
    return tmp_path, state


def test_aged_uploaded_segments_are_pruned(tmp_path):
    spool, state = aged_spool(tmp_path)
    assert prune_spool(spool, "demo", state, retention_s=300) == 4
    assert sorted(p.name for p in spool.glob("seg_*.ts")) == ["seg_000004.ts"]


def test_the_newest_segment_is_never_pruned(tmp_path):
    """With the playlist gone, ffmpeg resumes numbering from the highest index on
    disk; an empty spool would restart at 0 and overwrite the bucket."""
    spool, state = aged_spool(tmp_path)
    prune_spool(spool, "demo", state, retention_s=300)
    assert (spool / "seg_000004.ts").exists()


def test_a_lone_segment_is_kept(tmp_path):
    spool, state = aged_spool(tmp_path, count=1)
    assert prune_spool(spool, "demo", state, retention_s=300) == 0
    assert (spool / "seg_000000.ts").exists()


def test_segments_not_yet_uploaded_are_never_pruned(tmp_path):
    """Deleting one would lose it: nothing re-reads a segment ffmpeg has closed."""
    spool, state = aged_spool(tmp_path)
    state.uploaded.discard("demo/seg_000001.ts")
    prune_spool(spool, "demo", state, retention_s=300)
    assert (spool / "seg_000001.ts").exists()


def test_segments_uploaded_under_another_prefix_are_not_pruned(tmp_path):
    """Two streams share a volume; one must not delete the other's spool."""
    spool, state = aged_spool(tmp_path)
    assert prune_spool(spool, "other", state, retention_s=300) == 0
    assert len(list(spool.glob("seg_*.ts"))) == 5


def test_fresh_segments_are_kept(tmp_path):
    spool, state = aged_spool(tmp_path, age_s=0)
    assert prune_spool(spool, "demo", state, retention_s=300) == 0


def test_the_playlist_is_never_pruned(tmp_path):
    """ffmpeg appends to it on restart; losing it restarts numbering at zero."""
    spool, state = aged_spool(tmp_path)
    state.uploaded.add("demo/index.m3u8")
    prune_spool(spool, "demo", state, retention_s=300)
    assert (spool / PLAYLIST_NAME).exists()


def test_the_state_file_is_never_pruned(tmp_path):
    spool, state = aged_spool(tmp_path)
    save_state(spool, state)
    os.utime(spool / STATE_FILE, (0, 0))
    state.uploaded.add(f"demo/{STATE_FILE}")
    prune_spool(spool, "demo", state, retention_s=300)
    assert (spool / STATE_FILE).exists()


def test_tmp_files_are_never_pruned(tmp_path):
    """A .tmp file is a segment ffmpeg is still writing."""
    spool, state = aged_spool(tmp_path)
    partial = spool / "seg_000005.ts.tmp"
    partial.write_bytes(b"\x47")
    os.utime(partial, (0, 0))
    state.uploaded.add("demo/seg_000005.ts.tmp")
    prune_spool(spool, "demo", state, retention_s=300)
    assert partial.exists()


def test_retention_zero_still_keeps_the_newest(tmp_path):
    spool, state = aged_spool(tmp_path)
    prune_spool(spool, "demo", state, retention_s=0)
    assert [p.name for p in spool.glob("seg_*.ts")] == ["seg_000004.ts"]


def test_pruning_a_missing_spool_is_not_an_error(tmp_path):
    assert prunable_segments(tmp_path / "nope", "demo", SyncState(), 300) == []


# --- one prefix, one numbering sequence --------------------------------------
#
# The stream id is a hash of the camera URL, so the same camera lands on the same
# prefix every time. If the spool is gone but the bucket is not, ffmpeg restarts
# at seg_000000 and overwrites yesterday's objects while yesterday's manifest is
# still published — viewers get the old playlist pointing at the new video.


def test_a_spool_without_a_playlist_is_fresh(tmp_path):
    assert spool_is_fresh(tmp_path)


def test_a_spool_with_a_playlist_is_not_fresh(tmp_path):
    (tmp_path / PLAYLIST_NAME).write_text("#EXTM3U\n")
    assert not spool_is_fresh(tmp_path)


def test_a_pruned_spool_is_not_fresh(tmp_path):
    """Pruning keeps the playlist precisely so this stays distinguishable."""
    spool, state = aged_spool(tmp_path)
    prune_spool(spool, "demo", state, retention_s=0)
    assert not spool_is_fresh(spool)


def test_a_spool_with_only_a_state_file_is_not_fresh(tmp_path):
    """State alone would make the uploader skip segments it thinks are stored."""
    save_state(tmp_path, SyncState(uploaded={"demo/seg_000000.ts"}))
    assert not spool_is_fresh(tmp_path)


def test_purge_removes_everything_under_the_prefix():
    storage = FakeStorage(existing=["demo/index.m3u8", "demo/seg_000000.ts"])
    assert purge_prefix(storage, "demo") == 2
    assert storage.list_keys("demo/") == []


def test_purge_leaves_other_prefixes_alone():
    storage = FakeStorage(existing=["demo/seg_000000.ts", "other/seg_000000.ts"])
    purge_prefix(storage, "demo")
    assert storage.list_keys("other/") == ["other/seg_000000.ts"]


def test_purge_is_not_confused_by_a_prefix_that_is_a_name_prefix():
    """`demo` must not take `demo2` with it."""
    storage = FakeStorage(existing=["demo/seg_000000.ts", "demo2/seg_000000.ts"])
    purge_prefix(storage, "demo")
    assert storage.list_keys("demo2/") == ["demo2/seg_000000.ts"]


def test_purge_of_an_empty_prefix_is_not_an_error():
    assert purge_prefix(FakeStorage(), "demo") == 0


def test_pruning_does_not_disturb_the_upload_state(tmp_path):
    """Pruned segments must stay in `uploaded`, or the next pass re-uploads them."""
    spool, state = aged_spool(tmp_path)
    prune_spool(spool, "demo", state, retention_s=300)
    storage = FakeStorage()
    sync_once(spool, storage, "demo", state)
    assert not [k for k in storage.keys if k.endswith(".ts")]
