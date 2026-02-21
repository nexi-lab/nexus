"""Tests for WalletProvisioner — Issue #2180."""

from unittest.mock import MagicMock, patch

from nexus.factory.wallet import WalletProvisioner, create_wallet_provisioner


class TestWalletProvisioner:
    """WalletProvisioner class tests."""

    def test_lazy_client_creation(self) -> None:
        wp = WalletProvisioner(tb_address="127.0.0.1:3000", tb_cluster=0)
        assert wp._client is None

    def test_call_creates_client_and_account(self) -> None:
        mock_tb = MagicMock()
        mock_client = MagicMock()
        mock_client.create_accounts.return_value = []

        mock_tb.ClientSync.return_value = mock_client
        mock_tb.Account.return_value = MagicMock()
        mock_tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS = 0

        with (
            patch.dict("sys.modules", {"tigerbeetle": mock_tb}),
            patch.dict(
                "sys.modules",
                {
                    "nexus.bricks.pay.constants": MagicMock(
                        ACCOUNT_CODE_WALLET=1,
                        LEDGER_CREDITS=1,
                        make_tb_account_id=lambda z, a: 12345,
                    )
                },
            ),
        ):
            wp = WalletProvisioner(tb_address="127.0.0.1:3000", tb_cluster=0)
            wp("agent-1", "zone-1")
            assert wp._client is not None
            mock_client.create_accounts.assert_called_once()


class TestCreateWalletProvisioner:
    """create_wallet_provisioner() factory function tests."""

    def test_disabled_when_env_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = create_wallet_provisioner()
            assert result is None

    def test_disabled_when_pay_enabled_but_no_tigerbeetle(self) -> None:
        import builtins

        original_import = builtins.__import__

        def _selective_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "tigerbeetle":
                raise ImportError("No module named 'tigerbeetle'")
            return original_import(name, *args, **kwargs)

        with (
            patch.dict("os.environ", {"NEXUS_PAY_ENABLED": "true"}),
            patch("builtins.__import__", side_effect=_selective_import),
        ):
            result = create_wallet_provisioner()
            assert result is None

    def test_returns_provisioner_when_enabled(self) -> None:
        mock_tb = MagicMock()
        with (
            patch.dict("os.environ", {"NEXUS_PAY_ENABLED": "true"}),
            patch.dict("sys.modules", {"tigerbeetle": mock_tb}),
        ):
            result = create_wallet_provisioner()
            assert isinstance(result, WalletProvisioner)
