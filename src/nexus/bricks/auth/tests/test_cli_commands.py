from __future__ import annotations

import click


def test_auth_group_importable() -> None:
    from nexus.bricks.auth.cli_commands import auth

    assert isinstance(auth, click.Group)
    assert auth.name == "auth"


def test_auth_group_has_expected_subcommands() -> None:
    from nexus.bricks.auth.cli_commands import auth

    expected = {"list", "test", "connect", "disconnect", "doctor", "pool", "migrate"}
    assert expected.issubset(set(auth.commands.keys()))
