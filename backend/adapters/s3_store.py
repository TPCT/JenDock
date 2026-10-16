"""Small S3 object-store adapter used only by the worker."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import BinaryIO, Protocol

from backend.domain.errors import UpstreamError


class S3Client(Protocol):
    def upload_fileobj(self, *args: object, **kwargs: object) -> None: ...
    def head_object(self, **kwargs: object) -> dict[str, object]: ...
    def list_objects_v2(self, **kwargs: object) -> dict[str, object]: ...
    def delete_object(self, **kwargs: object) -> object: ...
    def put_object(self, **kwargs: object) -> object: ...


class S3Store:
    """S3 upload, verification, retention and connection-test operations."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def upload(self, key: str, stream: BinaryIO) -> None:
        try:
            self._client.upload_fileobj(
                stream, self._bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"}
            )
        except Exception as error:
            raise UpstreamError("S3 upload failed") from error

    def size(self, key: str) -> int:
        try:
            result = self._client.head_object(**{"Bucket": self._bucket, "Key": key})
            size = result.get("ContentLength", 0)
            if isinstance(size, bool) or not isinstance(size, int):
                raise UpstreamError("S3 returned an invalid object size")
            return size
        except Exception as error:
            raise UpstreamError("S3 verification failed") from error

    def keys(self, prefix: str) -> Iterator[str]:
        token: str | None = None
        while True:
            try:
                args: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
                if token is not None:
                    args["ContinuationToken"] = token
                result = self._client.list_objects_v2(**args)
            except Exception as error:
                raise UpstreamError("S3 listing failed") from error
            contents = result.get("Contents", [])
            if isinstance(contents, list):
                for item in contents:
                    if isinstance(item, dict) and isinstance(item.get("Key"), str):
                        yield item["Key"]
            token_value = result.get("NextContinuationToken")
            if not result.get("IsTruncated") or not isinstance(token_value, str):
                return
            token = token_value

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(**{"Bucket": self._bucket, "Key": key})
        except Exception as error:
            raise UpstreamError("S3 retention cleanup failed") from error

    def test(self, prefix: str, nonce: str) -> None:
        key = f"{prefix.rstrip('/')}/.jendock-test-{nonce}"
        try:
            self._client.put_object(
                **{
                    "Bucket": self._bucket,
                    "Key": key,
                    "Body": b"jendock s3 backup test",
                    "ServerSideEncryption": "AES256",
                }
            )
            listed = list(self.keys(key))
            if key not in listed or self.size(key) <= 0:
                raise UpstreamError("S3 test object could not be read back")
            self.delete(key)
        except Exception as error:
            with contextlib.suppress(UpstreamError):
                self.delete(key)
            if isinstance(error, UpstreamError):
                raise
            raise UpstreamError("S3 connection test failed") from error


def verify_and_retain_latest(store: S3Store, key: str, connection_prefix: str) -> int:
    """Verify a new object before deleting any previous object for its connection."""
    size = store.size(key)
    if size <= 0:
        raise UpstreamError("S3 verification returned an empty object")
    for previous in store.keys(connection_prefix):
        if previous != key:
            store.delete(previous)
    return size
