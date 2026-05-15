"""Unit tests for BackendAddress value class (#1293)."""

import pytest

from nexus.contracts.backend_address import BackendAddress


class TestBackendAddressParse:
    def test_type_only(self):
        addr = BackendAddress.parse("local")
        assert addr.backend_type == "local"
        assert addr.origins == ()
        assert not addr.has_origin

    def test_type_with_origin(self):
        addr = BackendAddress.parse("local@10.0.0.5:50051")
        assert addr.backend_type == "local"
        assert addr.origins == ("10.0.0.5:50051",)
        assert addr.has_origin

    def test_s3_with_origin(self):
        addr = BackendAddress.parse("s3@us-east-1.s3.example.com:443")
        assert addr.backend_type == "s3"
        assert addr.origins == ("us-east-1.s3.example.com:443",)

    def test_multi_origin(self):
        addr = BackendAddress.parse("local@10.0.0.1:50051,10.0.0.2:50051")
        assert addr.backend_type == "local"
        assert addr.origins == ("10.0.0.1:50051", "10.0.0.2:50051")
        assert addr.has_origin

    def test_multi_origin_three_nodes(self):
        addr = BackendAddress.parse("local@A:50051,B:50051,C:50051")
        assert addr.origins == ("A:50051", "B:50051", "C:50051")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            BackendAddress.parse("")


class TestBackendAddressBuild:
    def test_build_with_origin(self):
        addr = BackendAddress.build("local", "10.0.0.5:50051")
        assert addr.backend_type == "local"
        assert addr.origins == ("10.0.0.5:50051",)

    def test_build_without_origin(self):
        addr = BackendAddress.build("local")
        assert addr.backend_type == "local"
        assert addr.origins == ()


class TestBackendAddressWithOrigin:
    def test_add_new_origin(self):
        addr = BackendAddress.parse("local@10.0.0.1:50051")
        addr2 = addr.with_origin("10.0.0.2:50051")
        assert addr2.origins == ("10.0.0.1:50051", "10.0.0.2:50051")

    def test_add_existing_origin_idempotent(self):
        addr = BackendAddress.parse("local@10.0.0.1:50051")
        addr2 = addr.with_origin("10.0.0.1:50051")
        assert addr2 is addr  # same object, not a copy

    def test_add_to_empty(self):
        addr = BackendAddress.parse("local")
        addr2 = addr.with_origin("10.0.0.1:50051")
        assert addr2.origins == ("10.0.0.1:50051",)

    def test_chain_with_origin(self):
        addr = (
            BackendAddress.parse("local")
            .with_origin("A:50051")
            .with_origin("B:50051")
            .with_origin("C:50051")
        )
        assert addr.origins == ("A:50051", "B:50051", "C:50051")


class TestBackendAddressStr:
    def test_type_only(self):
        assert str(BackendAddress("local")) == "local"

    def test_type_with_single_origin(self):
        assert str(BackendAddress("local", ("10.0.0.5:50051",))) == "local@10.0.0.5:50051"

    def test_type_with_multi_origin(self):
        addr = BackendAddress("local", ("10.0.0.1:50051", "10.0.0.2:50051"))
        assert str(addr) == "local@10.0.0.1:50051,10.0.0.2:50051"

    def test_round_trip_single(self):
        raw = "s3@us-east-1.s3.example.com:443"
        assert str(BackendAddress.parse(raw)) == raw

    def test_round_trip_multi(self):
        raw = "local@10.0.0.1:50051,10.0.0.2:50051"
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

    def test_multi_origin_hashable(self):
        a = BackendAddress.parse("local@A:50051,B:50051")
        b = BackendAddress.parse("local@A:50051,B:50051")
        assert a == b
        assert hash(a) == hash(b)


class TestBackendAddressOriginMembership:
    """Test 'addr in origins' pattern used by federation resolvers."""

    def test_self_in_single_origin(self):
        addr = BackendAddress.parse("local@10.0.0.1:50051")
        assert "10.0.0.1:50051" in addr.origins
        assert "10.0.0.2:50051" not in addr.origins

    def test_self_in_multi_origin(self):
        addr = BackendAddress.parse("local@10.0.0.1:50051,10.0.0.2:50051")
        assert "10.0.0.1:50051" in addr.origins
        assert "10.0.0.2:50051" in addr.origins
        assert "10.0.0.3:50051" not in addr.origins

    def test_empty_origins(self):
        addr = BackendAddress.parse("local")
        assert "anything" not in addr.origins
