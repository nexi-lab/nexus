from __future__ import annotations

import pytest
from testkit.profiles import (
    ProfileCase,
    all_profile_cases,
    all_profile_params,
    local_profile_cases,
    local_profile_params,
    profile_params,
    remote_profile_cases,
    service_profile_cases,
)

from nexus.contracts.deployment_profile import DeploymentProfile


def test_all_profile_cases_cover_deployment_profiles_in_stable_order() -> None:
    assert [case.profile for case in all_profile_cases()] == [
        DeploymentProfile.CLUSTER,
        DeploymentProfile.EMBEDDED,
        DeploymentProfile.LITE,
        DeploymentProfile.SANDBOX,
        DeploymentProfile.FULL,
        DeploymentProfile.CLOUD,
        DeploymentProfile.REMOTE,
    ]


def test_profile_cases_mirror_deployment_profile_defaults() -> None:
    for case in all_profile_cases():
        assert isinstance(case, ProfileCase)
        assert case.id == case.profile.value
        assert case.expected_bricks == case.profile.default_bricks()
        assert case.expected_drivers == case.profile.default_drivers()


def test_profile_subsets_are_explicit() -> None:
    assert [case.profile for case in local_profile_cases()] == [
        DeploymentProfile.EMBEDDED,
        DeploymentProfile.LITE,
        DeploymentProfile.SANDBOX,
        DeploymentProfile.FULL,
    ]
    assert [case.profile for case in remote_profile_cases()] == [DeploymentProfile.REMOTE]
    assert [case.profile for case in service_profile_cases()] == [
        DeploymentProfile.CLUSTER,
        DeploymentProfile.CLOUD,
    ]


def test_profile_params_keep_stable_pytest_ids() -> None:
    params = all_profile_params()
    assert [param.id for param in params] == [case.id for case in all_profile_cases()]


def test_local_profile_params_keep_stable_pytest_ids() -> None:
    params = local_profile_params()
    assert [param.id for param in params] == [case.id for case in local_profile_cases()]


def test_profile_params_can_skip_external_service_cases() -> None:
    params = profile_params(service_profile_cases(), skip_external=True)
    by_id = {param.id: param for param in params}

    for profile_id in ("cluster", "cloud"):
        mark_names = [mark.name for mark in by_id[profile_id].marks]
        assert "skip" in mark_names


@pytest.mark.parametrize("case", all_profile_params())
def test_param_values_are_profile_cases(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
    assert case.expected_drivers == case.profile.default_drivers()
