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
    # Click 8.2 removed CliRunner(mix_stderr=...) and captures stderr
    # separately. The rejection is printed with err=True, so gather both
    # streams version-robustly (stdout in old mixed Click, stderr in
    # Click >= 8.2).
    runner = CliRunner()
    result = runner.invoke(nexusd_main, ["--profile", "remote"])
    combined = result.output or ""
    try:
        if result.stderr:
            combined += result.stderr
    except (ValueError, AttributeError):
        pass
    assert result.exit_code != 0
    assert "cannot run with profile='remote'" in combined
