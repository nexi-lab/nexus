"""Pure utility functions for agent context extraction and config creation.

Extracted from AgentRPCService (Issue #2133) to break the
core/ -> services/ import dependency. These are stateless functions
with no service-layer dependencies.

Issue #2960 C6: Shared helpers to eliminate duplication between
agent_service.py and agent_rpc_service.py.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_zone_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract zone_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("zone_id")
    return getattr(context, "zone_id", None)


def extract_user_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract user_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("user_id")
    return getattr(context, "user_id", None)


def create_agent_config_data(
    agent_id: str,
    name: str,
    user_id: str,
    description: str | None,
    created_at: str | None,
    metadata: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Build agent config data dictionary."""
    config_data: dict[str, Any] = {
        "agent_id": agent_id,
        "name": name,
        "user_id": user_id,
        "description": description,
        "created_at": created_at,
    }
    if metadata:
        config_data["metadata"] = metadata.copy()
    if api_key is not None:
        config_data["api_key"] = api_key
    return config_data


def provision_agent_identity(
    agent_id: str,
    agent: dict,
    key_service: Any,
    _logger: logging.Logger | None = None,
) -> str | None:
    """Provision Ed25519 keypair + DID for an agent.

    Shared by both AgentService and AgentRPCService.
    """
    _log = _logger or logger
    if not key_service:
        return None
    try:
        key_record = key_service.ensure_keypair(agent_id)
        agent["did"] = key_record.did
        agent["key_id"] = key_record.key_id
        _log.info("[KYA] Provisioned identity for agent %s (did=%s)", agent_id, key_record.did)
        return str(key_record.did)
    except Exception as e:
        _log.warning("[KYA] Failed to provision identity for agent %s: %s", agent_id, e)
        return None


def provision_agent_wallet(
    agent_id: str,
    zone_id: str,
    wallet_provisioner: Any,
    _logger: logging.Logger | None = None,
) -> None:
    """Provision a wallet for an agent.

    Shared by both AgentService and AgentRPCService.
    """
    _log = _logger or logger
    if wallet_provisioner is None:
        return
    try:
        wallet_provisioner(agent_id, zone_id)
        _log.info("[WALLET] Provisioned wallet for agent %s", agent_id)
    except Exception as e:
        _log.warning("[WALLET] Failed to provision wallet for agent %s: %s", agent_id, e)
