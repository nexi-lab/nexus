"""Daemon-side FULL profile behavior (Issue #4132).

- ``nexusd --profile remote`` is rejected (a daemon cannot be a thin
  client of another daemon).

Note on ``test_full_profile_banner``: nexusd has no ``--dry-run`` or
``--check-only`` flag, so the banner is printed inside the blocking serve
loop with no safe early-exit path available in unit scope.  The banner test
has been deliberately omitted per the plan's documented fallback: a flaky
test that starts a server is worse than no banner test.  The remote-profile
guard fires *before* the banner and already proves the CLI/guard wiring.
"""

from __future__ import annotations

from click.testing import CliRunner

from nexus.daemon.main import main as nexusd_main


def test_remote_profile_is_rejected() -> None:
    runner = CliRunner(mix_stderr=True)
    result = runner.invoke(nexusd_main, ["--profile", "remote"])
    assert result.exit_code != 0
    assert "cannot run with profile='remote'" in result.output
