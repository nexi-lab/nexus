"""Unit tests for S3Transport.get_mtime (Issue #4266 GC grace period)."""

from __future__ import annotations

import datetime as dt

from nexus.backends.transports.s3_transport import S3Transport


class FakeS3Client:
    def __init__(self, last_modified=None, raises: BaseException | None = None) -> None:
        self.last_modified = last_modified
        self.raises = raises

    def head_object(self, **_kwargs):
        if self.raises is not None:
            raise self.raises
        return {"LastModified": self.last_modified} if self.last_modified is not None else {}


def _transport(client) -> S3Transport:
    t = S3Transport.__new__(S3Transport)
    t.bucket_name = "b"
    t.s3_client = client
    return t


def test_get_mtime_returns_epoch_seconds():
    lm = dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.UTC)
    t = _transport(FakeS3Client(last_modified=lm))
    assert t.get_mtime("cas/aa/bb/aabb...") == lm.timestamp()


def test_get_mtime_returns_zero_when_last_modified_missing():
    t = _transport(FakeS3Client(last_modified=None))
    assert t.get_mtime("cas/aa/bb/aabb...") == 0.0


def test_get_mtime_returns_zero_on_client_error():
    from nexus.backends.transports.s3_transport import BotoClientError

    err = BotoClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    t = _transport(FakeS3Client(raises=err))
    assert t.get_mtime("missing/key") == 0.0
