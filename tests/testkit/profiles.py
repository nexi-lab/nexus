"""Shared deployment profile matrices for tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest


@dataclass(frozen=True)
class TestProfile:
    """Profile metadata used by parametrized tests."""

    __test__ = False

    name: str
    config: dict[str, Any]
    requires_server: bool = False
    requires_remote: bool = False
    requires_federation: bool = False
    reason: str | None = None

    @property
    def is_available(self) -> bool:
        return not (self.requires_server or self.requires_remote or self.requires_federation)

    @property
    def skip_reason(self) -> str | None:
        if self.is_available:
            return None
        if self.reason:
            return self.reason
        if self.requires_remote:
            return "requires remote URL and API key"
        if self.requires_federation:
            return "requires federation test environment"
        if self.requires_server:
            return "requires live Nexus server fixture"
        return "profile is not available in this test environment"


_PROFILES: dict[str, TestProfile] = {
    "slim": TestProfile("slim", {"profile": "slim"}),
    "sandbox": TestProfile("sandbox", {"profile": "sandbox"}),
    "embedded": TestProfile("embedded", {"profile": "embedded"}),
    "server": TestProfile(
        "server",
        {"profile": "full"},
        requires_server=True,
        reason="requires live Nexus server fixture",
    ),
    "remote": TestProfile(
        "remote",
        {"profile": "remote"},
        requires_remote=True,
        reason="requires remote URL and API key",
    ),
    "federation": TestProfile(
        "federation",
        {"profile": "cluster"},
        requires_federation=True,
        reason="requires federation test environment",
    ),
}


def profile_matrix(*names: str) -> tuple[TestProfile, ...]:
    """Return profile metadata in caller-specified order."""
    selected = names or tuple(_PROFILES)
    unknown = [name for name in selected if name not in _PROFILES]
    if unknown:
        known = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unknown test profile(s): {unknown}. Known profiles: {known}")
    return tuple(replace(_PROFILES[name], config=dict(_PROFILES[name].config)) for name in selected)


def pytest_profile_params(
    *names: str,
    include_unavailable: bool = False,
) -> list[Any]:
    """Return pytest params with stable IDs and skip marks for unavailable profiles."""
    params: list[Any] = []
    for profile in profile_matrix(*names):
        marks: tuple[Any, ...] = ()
        if not include_unavailable and not profile.is_available:
            reason = profile.skip_reason or "profile is not available in this test environment"
            marks = (pytest.mark.skip(reason=reason),)
        params.append(pytest.param(profile, id=f"profile={profile.name}", marks=marks))
    return params


__all__ = ["TestProfile", "profile_matrix", "pytest_profile_params"]
