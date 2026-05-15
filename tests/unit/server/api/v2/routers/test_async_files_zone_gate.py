"""Zone allow-list gate helper for file-op handlers (#3785)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from nexus.server.api.v2.routers.async_files import _gate_zone


def test_gate_passes_when_zone_in_set():
    auth = {"zone_set": ["eng", "ops"], "is_admin": False}
    _gate_zone(auth, "ops")  # no raise


def test_gate_passes_when_zone_id_fallback_in_set():
    """zone_set absent: falls back to single-element [zone_id]."""
    auth = {"zone_id": "eng", "is_admin": False}
    _gate_zone(auth, "eng")  # no raise


def test_gate_raises_403_when_zone_outside_set():
    auth = {"zone_set": ["eng"], "is_admin": False}
    with pytest.raises(HTTPException) as exc_info:
        _gate_zone(auth, "legal")
    assert exc_info.value.status_code == 403
    assert "legal" in exc_info.value.detail
    assert "allow-list" in exc_info.value.detail.lower()


def test_gate_admin_bypasses():
    auth = {"zone_set": ["eng"], "is_admin": True}
    _gate_zone(auth, "legal")  # no raise


def test_gate_empty_zone_set_falls_back_to_zone_id():
    """Edge case: zone_set empty list, zone_id present."""
    auth = {"zone_set": [], "zone_id": "eng", "is_admin": False}
    _gate_zone(auth, "eng")  # no raise


def test_gate_empty_zone_set_no_zone_id_rejects_anything():
    """Edge case: no zones at all → reject any explicit zone."""
    auth = {"zone_set": [], "zone_id": None, "is_admin": False}
    with pytest.raises(HTTPException) as exc_info:
        _gate_zone(auth, "eng")
    assert exc_info.value.status_code == 403
