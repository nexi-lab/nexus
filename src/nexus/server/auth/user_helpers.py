"""User management helper functions — backward compatibility re-exports.

User lookups have moved to: nexus.auth.user_queries
Zone membership has moved to: nexus.server.services.zone_membership

This file re-exports all symbols for backward compatibility.
"""


# User lookup functions (now in auth brick)
from nexus.auth.user_queries import (  # noqa: F401
    check_email_available,
    check_username_available,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    validate_user_uniqueness,
)

# Zone helper functions (canonical: core.zone_helpers, re-exported for backward compat)
from nexus.core.zone_helpers import (  # noqa: F401
    add_user_to_zone,
    can_invite_to_zone,
    get_user_zones,
    is_zone_admin,
    is_zone_group,
    is_zone_owner,
    parse_zone_from_group,
    remove_user_from_zone,
    user_belongs_to_zone,
    zone_group_id,
)
