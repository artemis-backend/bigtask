"""S3-compatible object storage. Works against AWS S3, Cloudflare R2 and MinIO.

R2 infers Content-Type when the caller omits it, and a wrong type makes the
player fail without an error message — so every put sets it explicitly.
"""

from pathlib import PurePosixPath
from typing import Protocol

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(PurePosixPath(name).suffix, DEFAULT_CONTENT_TYPE)


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...


class S3ObjectStorage:
    """Thin boto3 wrapper. R2 needs region_name='auto' and an explicit endpoint."""

    def __init__(
        self,
        *,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str | None = None,
        region: str = "auto",
        max_pool_connections: int = 16,
        client=None,
    ) -> None:
        self.bucket = bucket
        if client is not None:
            self._client = client
            return
        import boto3
        from botocore.config import Config

        # The pool must be at least as large as the number of uploader threads,
        # or they serialise on connections and the concurrency buys nothing.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(max_pool_connections=max_pool_connections),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
