"""Tests for shared gRPC channel defaults."""

from nexus.grpc.defaults import (
    MAX_CONTENT_MESSAGE_BYTES,
    MAX_METADATA_MESSAGE_BYTES,
    build_channel_options,
)


class TestGrpcDefaults:
    """Test gRPC default constants and option builder."""

    def test_content_message_limit_is_64mb(self) -> None:
        assert MAX_CONTENT_MESSAGE_BYTES == 64 * 1024 * 1024

    def test_metadata_message_limit_is_16mb(self) -> None:
        assert MAX_METADATA_MESSAGE_BYTES == 16 * 1024 * 1024

    def test_metadata_limit_smaller_than_content(self) -> None:
        assert MAX_METADATA_MESSAGE_BYTES < MAX_CONTENT_MESSAGE_BYTES

    def test_build_channel_options_default(self) -> None:
        options = build_channel_options()
        options_dict = dict(options)
        assert options_dict["grpc.max_send_message_length"] == MAX_CONTENT_MESSAGE_BYTES
        assert options_dict["grpc.max_receive_message_length"] == MAX_CONTENT_MESSAGE_BYTES
        assert options_dict["grpc.keepalive_time_ms"] == 30_000
        assert options_dict["grpc.keepalive_timeout_ms"] == 10_000
        assert options_dict["grpc.keepalive_permit_without_calls"] == 1
        assert options_dict["grpc.http2.max_pings_without_data"] == 0

    def test_build_channel_options_custom_message_size(self) -> None:
        options = build_channel_options(max_message_bytes=MAX_METADATA_MESSAGE_BYTES)
        options_dict = dict(options)
        assert options_dict["grpc.max_send_message_length"] == MAX_METADATA_MESSAGE_BYTES
        assert options_dict["grpc.max_receive_message_length"] == MAX_METADATA_MESSAGE_BYTES

    def test_build_channel_options_custom_keepalive(self) -> None:
        options = build_channel_options(keepalive_time_ms=10_000, keepalive_timeout_ms=5_000)
        options_dict = dict(options)
        assert options_dict["grpc.keepalive_time_ms"] == 10_000
        assert options_dict["grpc.keepalive_timeout_ms"] == 5_000

    def test_build_channel_options_returns_list_of_tuples(self) -> None:
        options = build_channel_options()
        assert isinstance(options, list)
        for item in options:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], int)

    def test_build_channel_options_has_six_entries(self) -> None:
        options = build_channel_options()
        assert len(options) == 6
