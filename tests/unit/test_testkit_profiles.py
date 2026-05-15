"""Tests for shared test profile matrices."""

from __future__ import annotations

import typing

import pytest


def test_profile_matrix_returns_named_profiles() -> None:
    from tests.testkit.profiles import profile_matrix

    profiles = profile_matrix("slim", "sandbox")

    assert [profile.name for profile in profiles] == ["slim", "sandbox"]
    assert profiles[0].config["profile"] == "slim"
    assert profiles[1].config["profile"] == "sandbox"
    assert profiles[0].is_available is True
    assert profiles[1].is_available is True


def test_profile_matrix_defaults_to_all_profiles() -> None:
    from tests.testkit.profiles import profile_matrix

    profiles = profile_matrix()

    assert [profile.name for profile in profiles] == [
        "slim",
        "sandbox",
        "embedded",
        "server",
        "remote",
        "federation",
    ]


def test_profile_matrix_returns_fresh_config_dicts() -> None:
    from tests.testkit.profiles import profile_matrix

    profile = profile_matrix("slim")[0]
    profile.config["profile"] = "mutated"
    profile.config["extra"] = "leaked"

    try:
        fresh_profile = profile_matrix("slim")[0]

        assert fresh_profile.config == {"profile": "slim"}
    finally:
        profile.config.clear()
        profile.config["profile"] = "slim"


def test_profile_matrix_unknown_profile_raises() -> None:
    from tests.testkit.profiles import profile_matrix

    with pytest.raises(ValueError, match="Unknown test profile"):
        profile_matrix("does-not-exist")


def test_pytest_profile_params_have_stable_ids() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("slim", "sandbox")

    assert [param.id for param in params] == ["profile=slim", "profile=sandbox"]


def test_pytest_profile_params_type_hints_resolve_without_pytest_internals() -> None:
    from tests.testkit.profiles import pytest_profile_params

    hints = typing.get_type_hints(pytest_profile_params)

    assert hints["return"] == list[typing.Any]


def test_unavailable_profiles_are_skipped_by_default() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("remote", "federation")

    assert [param.id for param in params] == ["profile=remote", "profile=federation"]
    assert all(param.marks for param in params)
    reasons = [mark.kwargs["reason"] for param in params for mark in param.marks]
    assert any("remote URL" in reason for reason in reasons)
    assert any("federation" in reason for reason in reasons)


def test_include_unavailable_returns_unmarked_params() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("remote", include_unavailable=True)

    assert params[0].id == "profile=remote"
    assert params[0].marks == ()
