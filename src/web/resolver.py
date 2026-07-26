"""Turn the "path to the broadcast" a user types into a manifest URL.

The spec never says what that path is, so both readings are accepted: a stream
identifier, or a ready-made manifest URL.
"""

import re
from urllib.parse import urlparse

PLAYLIST_NAME = "index.m3u8"
_STREAM_ID_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class InvalidSource(ValueError):
    """The submitted path is neither a usable stream id nor an allowed URL."""


def resolve_manifest_url(src: str, public_base_url: str) -> str:
    src = (src or "").strip()
    if not src:
        raise InvalidSource("source is empty")

    scheme = urlparse(src).scheme
    if scheme:
        if scheme not in _ALLOWED_SCHEMES:
            raise InvalidSource(f"scheme {scheme!r} is not allowed, use http or https")
        return src

    if ".." in src:
        raise InvalidSource("'..' is not allowed in a stream id")
    if not _STREAM_ID_RE.match(src):
        raise InvalidSource(
            "stream id may contain only letters, digits, dot, dash and underscore"
        )

    return f"{public_base_url.rstrip('/')}/{src}/{PLAYLIST_NAME}"
