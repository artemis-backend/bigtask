"""S3-compatible object storage. Works against AWS S3, Cloudflare R2 and MinIO.

R2 infers Content-Type when the caller omits it, and a wrong type makes the
player fail without an error message — so every put sets it explicitly.
"""

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol

# S3 DeleteObjects takes at most 1000 keys per call.
DELETE_BATCH = 1000

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

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete(self, keys: Sequence[str]) -> None: ...


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

    def list_keys(self, prefix: str) -> list[str]:
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        return [obj["Key"] for page in pages for obj in page.get("Contents", [])]

    def delete(self, keys: Sequence[str]) -> None:
        for start in range(0, len(keys), DELETE_BATCH):
            batch = keys[start:start + DELETE_BATCH]
            self._client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )
