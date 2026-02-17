"""Backward-compatibility shim — use nexus.auth.zone_helpers instead."""

from nexus.auth.constants import PERSONAL_EMAIL_DOMAINS, RESERVED_ZONE_IDS
from nexus.auth.zone_helpers import (
    create_zone,
    get_zone_strategy_from_email,
    is_personal_email_domain,
    is_zone_id_available,
    normalize_to_slug,
    suggest_zone_id,
    validate_zone_id,
)

__all__ = [
    "PERSONAL_EMAIL_DOMAINS",
    "RESERVED_ZONE_IDS",
    "is_personal_email_domain",
    "get_zone_strategy_from_email",
    "validate_zone_id",
    "is_zone_id_available",
    "suggest_zone_id",
    "normalize_to_slug",
    "create_zone",
]
