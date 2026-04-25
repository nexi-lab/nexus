"""Issue #3897 — DELETE /api/zones/root must be rejected at the HTTP layer.

Defense in depth: the lifecycle service already raises ValueError for the
reserved zone, but the route also short-circuits with HTTP 403 so the
admin/owner UI gets a clear error instead of a 500 from the lifecycle.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.auth.zone_routes import delete_zone_endpoint


@pytest.mark.asyncio
async def test_delete_root_zone_rejected_with_403():
    """Endpoint short-circuits to 403 before touching auth/lifecycle."""
    # `auth` is unused on the ROOT_ZONE_ID path; a stand-in MagicMock
    # avoids dragging in a real DatabaseLocalAuth.
    with pytest.raises(HTTPException) as exc:
        await delete_zone_endpoint(
            zone_id=ROOT_ZONE_ID,
            auth_result={"subject_id": "admin", "is_admin": True},
            auth=MagicMock(),
        )
    assert exc.value.status_code == 403
    assert ROOT_ZONE_ID in exc.value.detail
    assert "reserved" in exc.value.detail.lower()
