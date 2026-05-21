from unittest.mock import MagicMock

import pytest

from nexus.bricks.rebac.checker import PermissionChecker
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext, Permission


def test_root_multizone_zone_perms_grant_short_circuits_rebac() -> None:
    enforcer = MagicMock()
    enforcer._effective_zone_perms.return_value = (("company", "r"), ("shared", "rw"))
    enforcer.check_owner.return_value = False

    checker = PermissionChecker(
        permission_enforcer=enforcer,
        metadata_store=MagicMock(),
        default_context=OperationContext(user_id="default", groups=[]),
        enforce_permissions=True,
    )
    ctx = OperationContext(
        user_id="sandbox-token",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        zone_perms=(("company", "r"), ("shared", "rw")),
    )

    checker.check("/zone/shared/note.txt", Permission.WRITE, ctx)

    enforcer.check_owner.assert_not_called()


def test_root_multizone_zone_perms_deny_readonly_write() -> None:
    enforcer = MagicMock()
    enforcer._effective_zone_perms.return_value = (("company", "r"), ("shared", "rw"))

    checker = PermissionChecker(
        permission_enforcer=enforcer,
        metadata_store=MagicMock(),
        default_context=OperationContext(user_id="default", groups=[]),
        enforce_permissions=True,
    )
    ctx = OperationContext(
        user_id="sandbox-token",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        zone_perms=(("company", "r"), ("shared", "rw")),
    )

    with pytest.raises(PermissionError, match="read-only"):
        checker.check("/zone/company/no.txt", Permission.WRITE, ctx)
