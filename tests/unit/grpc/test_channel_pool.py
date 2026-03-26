"""Unit tests for PeerChannelPool."""

from unittest.mock import MagicMock, patch

from nexus.grpc.channel_pool import PeerChannelPool


class TestPeerChannelPool:
    def test_get_creates_channel(self) -> None:
        """First get() creates a channel via build_peer_channel."""
        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            mock_ch = MagicMock()
            mock_build.return_value = mock_ch

            pool = PeerChannelPool()
            ch = pool.get("10.0.0.1:50051")

            assert ch is mock_ch
            mock_build.assert_called_once_with("10.0.0.1:50051", None)

    def test_get_reuses_channel(self) -> None:
        """Second get() for same address returns cached channel."""
        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            mock_ch = MagicMock()
            mock_build.return_value = mock_ch

            pool = PeerChannelPool()
            ch1 = pool.get("10.0.0.1:50051")
            ch2 = pool.get("10.0.0.1:50051")

            assert ch1 is ch2
            assert mock_build.call_count == 1

    def test_different_addresses_different_channels(self) -> None:
        """Different addresses get different channels."""
        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            ch_a = MagicMock()
            ch_b = MagicMock()
            mock_build.side_effect = [ch_a, ch_b]

            pool = PeerChannelPool()
            result_a = pool.get("10.0.0.1:50051")
            result_b = pool.get("10.0.0.2:50051")

            assert result_a is ch_a
            assert result_b is ch_b
            assert mock_build.call_count == 2

    def test_close_all(self) -> None:
        """close_all() closes all channels and clears the pool."""
        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            ch_a = MagicMock()
            ch_b = MagicMock()
            mock_build.side_effect = [ch_a, ch_b]

            pool = PeerChannelPool()
            pool.get("10.0.0.1:50051")
            pool.get("10.0.0.2:50051")

            pool.close_all()

            ch_a.close.assert_called_once()
            ch_b.close.assert_called_once()
            # Pool is cleared — next get() creates new channel
            ch_c = MagicMock()
            mock_build.side_effect = [ch_c]
            assert pool.get("10.0.0.1:50051") is ch_c

    def test_set_tls_config(self) -> None:
        """set_tls_config() stores config for future channels."""
        pool = PeerChannelPool()
        mock_tls = MagicMock()
        pool.set_tls_config(mock_tls)

        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            mock_ch = MagicMock()
            mock_build.return_value = mock_ch

            pool.get("10.0.0.1:50051")
            mock_build.assert_called_once_with("10.0.0.1:50051", mock_tls)

    def test_tls_config_at_init(self) -> None:
        """TLS config can be passed at init time."""
        mock_tls = MagicMock()
        pool = PeerChannelPool(tls_config=mock_tls)

        with patch("nexus.grpc.channel_pool.build_peer_channel") as mock_build:
            mock_ch = MagicMock()
            mock_build.return_value = mock_ch

            pool.get("10.0.0.1:50051")
            mock_build.assert_called_once_with("10.0.0.1:50051", mock_tls)
