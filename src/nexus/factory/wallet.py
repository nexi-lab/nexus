"""WalletProvisioner — sync TigerBeetle wallet creation for agent registration."""

import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class WalletProvisioner:
    """Sync wallet provisioner for NexusFS agent registration.

    Creates TigerBeetle wallet accounts on demand.  The client is lazily
    initialised on first call and reused.  Account creation is idempotent
    (safe to call multiple times for the same agent).
    """

    def __init__(self, tb_address: str, tb_cluster: int) -> None:
        self._tb_address = tb_address
        self._tb_cluster = tb_cluster
        self._client: Any = None

    def __call__(self, agent_id: str, zone_id: str = ROOT_ZONE_ID) -> None:
        """Create TigerBeetle account for *agent_id*. Idempotent."""
        import tigerbeetle as tb

        from nexus.bricks.pay.constants import (
            ACCOUNT_CODE_WALLET,
            LEDGER_CREDITS,
            make_tb_account_id,
        )

        if self._client is None:
            self._client = tb.ClientSync(
                cluster_id=self._tb_cluster,
                replica_addresses=self._tb_address,
            )

        tb_id = make_tb_account_id(zone_id, agent_id)
        account = tb.Account(
            id=tb_id,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_WALLET,
            flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS,
        )

        client = self._client
        assert client is not None
        errors = client.create_accounts([account])
        # Ignore EXISTS (21) — idempotent operation
        if errors and errors[0].result not in (0, 21):
            raise RuntimeError(f"TigerBeetle account creation failed: {errors[0].result}")


def create_wallet_provisioner() -> WalletProvisioner | None:
    """Create a WalletProvisioner if TigerBeetle is available and pay is enabled.

    Returns None if NEXUS_PAY_ENABLED is not set or tigerbeetle is not installed.
    """
    import os

    tb_address = os.environ.get("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")
    tb_cluster = int(os.environ.get("TIGERBEETLE_CLUSTER_ID", "0"))
    pay_enabled = os.environ.get("NEXUS_PAY_ENABLED", "").lower() in ("true", "1", "yes")

    if not pay_enabled:
        logger.debug("[WALLET] NEXUS_PAY_ENABLED not set, wallet provisioner disabled")
        return None

    try:
        import tigerbeetle as _tb  # noqa: F401 — verify availability
    except ImportError:
        logger.debug("[WALLET] tigerbeetle package not installed, wallet provisioner disabled")
        return None

    logger.info("[WALLET] Wallet provisioner enabled (TigerBeetle @ %s)", tb_address)
    return WalletProvisioner(tb_address=tb_address, tb_cluster=tb_cluster)
