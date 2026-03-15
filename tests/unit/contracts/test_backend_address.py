"""Unit tests for BackendAddress value class (#1293)."""

import pytest

from nexus.contracts.backend_address import BackendAddress


class TestBackendAddressParse:
    def test_type_only(self):
        addr = BackendAddress.parse("local")
        assert addr.backend_type == "local"
        assert addr.origin is None
        assert not addr.has_origin

    def test_type_with_origin(self):
        addr = BackendAddress.parse("local@10.0.0.5:50051")
        assert addr.backend_type == "local"
        assert addr.origin == "10.0.0.5:50051"
        assert addr.has_origin

    def test_s3_with_origin(self):
        addr = BackendAddress.parse("s3@us-east-1.s3.example.com:443")
        assert addr.backend_type == "s3"
        assert addr.origin == "us-east-1.s3.example.com:443"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            BackendAddress.parse("")


class TestBackendAddressBuild:
    def test_build_with_origin(self):
        addr = BackendAddress.build("local", "10.0.0.5:50051")
        assert addr.backend_type == "local"
        assert addr.origin == "10.0.0.5:50051"

    def test_build_without_origin(self):
        addr = BackendAddress.build("local")
        assert addr.backend_type == "local"
        assert addr.origin is None


class TestBackendAddressStr:
    def test_type_only(self):
        assert str(BackendAddress("local")) == "local"

    def test_type_with_origin(self):
        assert str(BackendAddress("local", "10.0.0.5:50051")) == "local@10.0.0.5:50051"

    def test_round_trip(self):
        raw = "s3@us-east-1.s3.example.com:443"
        assert str(BackendAddress.parse(raw)) == raw


class TestBackendAddressFrozen:
    def test_immutable(self):
        addr = BackendAddress.parse("local@10.0.0.5:50051")
        with pytest.raises(AttributeError):
            addr.backend_type = "s3"

    def test_hashable(self):
        a = BackendAddress.parse("local@10.0.0.5:50051")
        b = BackendAddress.parse("local@10.0.0.5:50051")
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1
