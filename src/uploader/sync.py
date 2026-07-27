"""Push the local HLS spool into object storage.

Two rules carry the correctness of this module:

1. A segment is uploaded only once the playlist lists it. ffmpeg writes the
   playlist entry after closing the segment, so "listed" is the signal that the
   file is complete — scanning the directory instead would eventually publish a
   half-written segment, and nothing would ever go back and fix it.
2. The playlist is uploaded last, and only when every segment it references is
   already in the bucket. Otherwise viewers get a manifest pointing at 404s.

Once a segment is in the bucket the local copy has no reader — the player is
served from object storage — so it is pruned. The playlist itself is never
pruned: it is what ffmpeg appends to on restart.
"""

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from uploader.storage import ObjectStorage, content_type_for

PLAYLIST_NAME = "index.m3u8"
STATE_FILE = ".uploaded.json"

# Segments upload concurrently because the cost of one is a round trip, not
# bandwidth: a 120 KB segment takes ~0.67 s to store, of which the transfer is a
# small fraction. Uploading them one at a time therefore leaves the link idle and
# lets the published manifest fall minutes behind the live edge.
DEFAULT_WORKERS = 8

# How long an uploaded segment stays on disk before it is dropped. It buys
# nothing for playback — that reads from the bucket — only a margin for anything
# still holding the file open, so it is short.
DEFAULT_RETENTION_S = 300.0

# Pruning walks the spool directory, so it does not run on every upload cycle.
PRUNE_EVERY_S = 30.0

log = logging.getLogger(__name__)


@dataclass
class SyncState:
    uploaded: set[str] = field(default_factory=set)
    playlist_digest: str | None = None


def segments_in_playlist(playlist_text: str) -> list[str]:
    """Segment names, in playlist order. Tags, comments and blanks are skipped."""
    return [
        stripped
        for line in playlist_text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def load_state(spool: Path) -> SyncState:
    path = Path(spool) / STATE_FILE
    try:
        raw = json.loads(path.read_text())
        return SyncState(
            uploaded=set(raw.get("uploaded", [])),
            playlist_digest=raw.get("playlist_digest"),
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        # A missing or corrupt state file costs re-uploads, not correctness.
        return SyncState()


def save_state(spool: Path, state: SyncState) -> None:
    path = Path(spool) / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "uploaded": sorted(state.uploaded),
        "playlist_digest": state.playlist_digest,
    }))
    tmp.replace(path)


def _key(prefix: str, name: str) -> str:
    return f"{prefix.strip('/')}/{name}"


def _put_segment(spool: Path, storage: ObjectStorage, key: str, name: str) -> str | None:
    """The uploaded key, or None if this segment must be retried next pass."""
    try:
        data = (spool / name).read_bytes()
    except (FileNotFoundError, OSError):
        # Listed but not readable yet; the playlist must wait for it.
        return None
    try:
        storage.put(key, data, content_type_for(name))
    except Exception:
        log.exception("upload failed for %s", key)
        return None
    return key


def sync_once(
    spool: Path,
    storage: ObjectStorage,
    prefix: str,
    state: SyncState,
    workers: int = DEFAULT_WORKERS,
) -> SyncState:
    spool = Path(spool)
    try:
        playlist_bytes = (spool / PLAYLIST_NAME).read_bytes()
    except (FileNotFoundError, OSError):
        return state

    names = segments_in_playlist(playlist_bytes.decode("utf-8", "replace"))
    pending = [
        (name, _key(prefix, name))
        for name in names
        if not name.endswith(".tmp") and _key(prefix, name) not in state.uploaded
    ]

    complete = True
    if pending:
        # Order among segments is free to vary: nothing may read them until the
        # playlist is published, and that happens only once all of them landed.
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            results = pool.map(
                lambda item: _put_segment(spool, storage, item[1], item[0]), pending
            )
            for key in results:
                if key is None:
                    complete = False
                else:
                    state.uploaded.add(key)

    if not complete:
        return state

    digest = hashlib.sha256(playlist_bytes).hexdigest()
    if digest == state.playlist_digest:
        return state
    try:
        storage.put(_key(prefix, PLAYLIST_NAME), playlist_bytes,
                    content_type_for(PLAYLIST_NAME))
    except Exception:
        log.exception("playlist upload failed")
        return state
    state.playlist_digest = digest
    return state


def prunable_segments(
    spool: Path,
    prefix: str,
    state: SyncState,
    retention_s: float,
    now: float | None = None,
) -> list[Path]:
    """Local segments safe to delete: uploaded, aged out, and not the newest one.

    Being in `state.uploaded` is the only evidence that a segment is durable, so
    nothing else is ever a candidate. The newest uploaded segment is kept whatever
    its age, because when the playlist is lost ffmpeg picks its next segment
    number from the highest index still on disk — deleting the whole spool would
    restart numbering and overwrite the DVR history already in the bucket.
    """
    spool = Path(spool)
    now = time.time() if now is None else now
    try:
        names = sorted(entry.name for entry in spool.iterdir() if entry.is_file())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []

    uploaded = [
        name for name in names
        if name not in (PLAYLIST_NAME, STATE_FILE)
        and not name.endswith(".tmp")
        and _key(prefix, name) in state.uploaded
    ]
    if len(uploaded) < 2:
        return []

    out = []
    for name in uploaded[:-1]:
        path = spool / name
        try:
            aged = now - path.stat().st_mtime >= retention_s
        except OSError:
            continue
        if aged:
            out.append(path)
    return out


def prune_spool(
    spool: Path,
    prefix: str,
    state: SyncState,
    retention_s: float,
    now: float | None = None,
) -> int:
    """Delete what `prunable_segments` selects. Returns how many went."""
    removed = 0
    for path in prunable_segments(spool, prefix, state, retention_s, now):
        try:
            path.unlink()
        except OSError:
            log.warning("could not prune %s", path)
            continue
        removed += 1
    return removed


def spool_is_fresh(spool: Path) -> bool:
    """True when capture is about to start a new numbering sequence from zero.

    ffmpeg picks its next segment number from the playlist, so a spool without
    one restarts at `seg_000000` regardless of what the bucket already holds.
    The state file is checked too: it alone would let the uploader skip segments
    whose keys it believes are already stored.
    """
    spool = Path(spool)
    return not (spool / PLAYLIST_NAME).exists() and not (spool / STATE_FILE).exists()


def purge_prefix(storage: ObjectStorage, prefix: str) -> int:
    """Drop every object under the prefix. Returns how many went."""
    keys = storage.list_keys(f"{prefix.strip('/')}/")
    if keys:
        storage.delete(keys)
    return len(keys)


def run_forever(
    spool: Path,
    storage: ObjectStorage,
    prefix: str,
    interval: float,
    workers: int = DEFAULT_WORKERS,
    retention_s: float = DEFAULT_RETENTION_S,
    fresh: bool | None = None,
) -> None:
    """`fresh` says whether capture is starting a new numbering sequence.

    Callers that launch capture themselves must decide it *before* ffmpeg runs:
    once ffmpeg writes the first playlist the evidence is gone. Left as None the
    spool is inspected here, which is only sound when capture has not started yet.
    """
    # A prefix holds exactly one numbering sequence. Starting a second one over
    # the top of an old recording overwrites its segments while leaving the old
    # manifest in place, so viewers get yesterday's playlist pointing at today's
    # video until the new manifest lands. Clearing first is the only consistent
    # outcome; keeping both is not on offer, because the keys collide.
    if spool_is_fresh(spool) if fresh is None else fresh:
        try:
            dropped = purge_prefix(storage, prefix)
        except Exception:
            log.exception("could not clear %s before a fresh capture", prefix)
        else:
            if dropped:
                log.warning("cleared %s stale objects under %s: capture restarts "
                            "numbering from zero", dropped, prefix)

    state = load_state(spool)
    last_prune = time.monotonic()
    while True:
        before = (len(state.uploaded), state.playlist_digest)
        state = sync_once(spool, storage, prefix, state, workers)
        if (len(state.uploaded), state.playlist_digest) != before:
            save_state(spool, state)

        if retention_s > 0 and time.monotonic() - last_prune >= PRUNE_EVERY_S:
            last_prune = time.monotonic()
            removed = prune_spool(spool, prefix, state, retention_s)
            if removed:
                log.info("pruned %s uploaded segments from the spool", removed)

        time.sleep(interval)
