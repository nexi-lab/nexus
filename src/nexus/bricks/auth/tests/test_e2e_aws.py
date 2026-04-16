"""Nightly end-to-end test with real AWS CLI.

Gated by TEST_WITH_REAL_AWS_CLI=1 env var. Skipped in default CI.
Creates a temp HOME with dummy AWS credentials, then asserts the
full sync + auth list flow works.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
from nexus.bricks.auth.external_sync.registry import AdapterRegistry
from nexus.bricks.auth.profile import InMemoryAuthProfileStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_AWS_CLI"),
    reason="Requires TEST_WITH_REAL_AWS_CLI=1 and aws CLI installed",
)

# ---------------------------------------------------------------------------
# Fixture: temp HOME with dummy AWS credentials + config
# ---------------------------------------------------------------------------

_CREDENTIALS = """\
[default]
aws_access_key_id = AKIATESTEXAMPLE123456
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYTESTKEY

[staging]
aws_access_key_id = AKIASTAGINGEXAMPLE789
aws_secret_access_key = stagingSecretAccessKey/EXAMPLEVALUE
"""

_CONFIG = """\
[default]
region = us-east-1

[profile staging]
region = eu-west-1
"""


@pytest.fixture()
def aws_home(tmp_path: Path) -> Path:
    """Create a temp dir with ~/.aws/credentials and ~/.aws/config."""
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()

    (aws_dir / "credentials").write_text(_CREDENTIALS, encoding="utf-8")
    (aws_dir / "config").write_text(_CONFIG, encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# TestRealAwsCli
# ---------------------------------------------------------------------------


class TestRealAwsCli:
    def test_aws_cli_is_installed(self) -> None:
        assert shutil.which("aws") is not None, "aws CLI binary not found on PATH"

    async def test_full_sync_and_list(
        self, aws_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / ".aws" / "credentials"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / ".aws" / "config"))

        adapter = AwsCliSyncAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([adapter], store, startup_timeout=5.0)

        results = await registry.startup()

        assert "aws-cli" in results
        assert results["aws-cli"].error is None

        profiles = store.list()
        assert len(profiles) == 2

        names = {p.account_identifier for p in profiles}
        assert names == {"default", "staging"}

        for p in profiles:
            assert p.backend == "external-cli"

    async def test_resolve_credential_from_file(
        self, aws_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / ".aws" / "credentials"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / ".aws" / "config"))

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/default")

        assert cred.kind == "api_key"
        assert cred.api_key == "AKIATESTEXAMPLE123456"
