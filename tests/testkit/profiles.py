"""Deployment profile matrices for Nexus tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile


@dataclass(frozen=True, slots=True)
class ProfileCase:
    """Explicit deployment-profile case for parametrized tests."""

    profile: DeploymentProfile
    id: str
    expected_bricks: frozenset[str]
    expected_drivers: frozenset[str]
    external_services: tuple[str, ...] = ()
    marks: tuple[pytest.MarkDecorator, ...] = ()

    def param(self, *, skip_external: bool = False) -> object:
        marks = list(self.marks)
        if skip_external and self.external_services:
            services = ", ".join(self.external_services)
            marks.append(
                pytest.mark.skip(
                    reason=f"profile {self.id} requires optional service(s): {services}"
                )
            )
        return pytest.param(self, id=self.id, marks=marks)


_ALL_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.CLUSTER,
    DeploymentProfile.EMBEDDED,
    DeploymentProfile.LITE,
    DeploymentProfile.SANDBOX,
    DeploymentProfile.FULL,
    DeploymentProfile.CLOUD,
    DeploymentProfile.REMOTE,
)

_LOCAL_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.EMBEDDED,
    DeploymentProfile.LITE,
    DeploymentProfile.SANDBOX,
    DeploymentProfile.FULL,
)

_REMOTE_PROFILES: tuple[DeploymentProfile, ...] = (DeploymentProfile.REMOTE,)

_SERVICE_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.CLUSTER,
    DeploymentProfile.CLOUD,
)

_EXTERNAL_SERVICES: dict[DeploymentProfile, tuple[str, ...]] = {
    DeploymentProfile.CLUSTER: ("federation",),
    DeploymentProfile.CLOUD: ("postgres", "redis", "nats"),
}


def profile_case(profile: DeploymentProfile) -> ProfileCase:
    """Build a `ProfileCase` from the production profile defaults."""

    return ProfileCase(
        profile=profile,
        id=profile.value,
        expected_bricks=profile.default_bricks(),
        expected_drivers=profile.default_drivers(),
        external_services=_EXTERNAL_SERVICES.get(profile, ()),
    )


def profile_cases(profiles: Iterable[DeploymentProfile]) -> tuple[ProfileCase, ...]:
    """Build profile cases in the caller-provided order."""

    return tuple(profile_case(profile) for profile in profiles)


def all_profile_cases() -> tuple[ProfileCase, ...]:
    """Return every known deployment profile in stable enum order."""

    return profile_cases(_ALL_PROFILES)


def local_profile_cases() -> tuple[ProfileCase, ...]:
    """Return profiles that run without remote/client or service-cluster semantics."""

    return profile_cases(_LOCAL_PROFILES)


def remote_profile_cases() -> tuple[ProfileCase, ...]:
    """Return remote-client profile cases."""

    return profile_cases(_REMOTE_PROFILES)


def service_profile_cases() -> tuple[ProfileCase, ...]:
    """Return profiles associated with federation or external services."""

    return profile_cases(_SERVICE_PROFILES)


def profile_params(
    cases: Iterable[ProfileCase],
    *,
    skip_external: bool = False,
) -> list[object]:
    """Convert profile cases into pytest parameters with stable IDs."""

    return [case.param(skip_external=skip_external) for case in cases]


def all_profile_params(*, skip_external: bool = False) -> list[object]:
    """Return pytest params for every deployment profile."""

    return profile_params(all_profile_cases(), skip_external=skip_external)


def local_profile_params(*, skip_external: bool = False) -> list[object]:
    """Return pytest params for local deployment profiles."""

    return profile_params(local_profile_cases(), skip_external=skip_external)
