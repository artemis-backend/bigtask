"""Push the local HLS spool into object storage.

Two rules carry the correctness of this module:

1. A segment is uploaded only once the playlist lists it. ffmpeg writes the
   playlist entry after closing the segment, so "listed" is the signal that the
   file is complete — scanning the directory instead would eventually publish a
   half-written segment, and nothing would ever go back and fix it.
2. The playlist is uploaded last, and only when every segment it references is
   already in the bucket. Otherwise viewers get a manifest pointing at 404s.
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


def run_forever(
    spool: Path,
    storage: ObjectStorage,
    prefix: str,
    interval: float,
    workers: int = DEFAULT_WORKERS,
) -> None:
    state = load_state(spool)
    while True:
        before = (len(state.uploaded), state.playlist_digest)
        state = sync_once(spool, storage, prefix, state, workers)
        if (len(state.uploaded), state.playlist_digest) != before:
            save_state(spool, state)
        time.sleep(interval)
