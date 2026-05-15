"""AWS CLI sync adapter — discovers profiles from ~/.aws/credentials + config.

FileAdapter subclass. All I/O, error handling, and retry logic lives in the
FileAdapter base class.

Phase 2 limitation: only discovers profiles with inline ``aws_access_key_id``.
Profiles defined by ``role_arn``, ``source_profile``, ``credential_process``,
or SSO fields in ``~/.aws/config`` are not yet discovered. These config-only
profile types are planned for Phase 3 alongside gcloud/gh adapters.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class AwsCliSyncAdapter(FileAdapter):
    """Discovers AWS profiles from credentials/config files."""

    adapter_name = "aws-cli"

    def paths(self) -> list[Path]:
        return [
            Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")).expanduser(),
            Path(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config")).expanduser(),
        ]

    def parse_file(self, _path: Path, content: str) -> list[SyncedProfile]:
        parser = configparser.ConfigParser()
        parser.read_string(content)

        profiles: list[SyncedProfile] = []
        for section in parser.sections():
            if not parser.has_option(section, "aws_access_key_id"):
                continue

            # ~/.aws/config uses "profile <name>" sections (except [default])
            name = section
            if name.startswith("profile "):
                name = name[len("profile ") :]

            profiles.append(
                SyncedProfile(
                    provider="s3",
                    account_identifier=name,
                    backend_key=f"aws-cli/{name}",
                    source="aws-cli",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        """Re-read AWS config files and extract credential for one profile."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            parser = configparser.ConfigParser()
            parser.read_string(content)

            # Try exact section name, then "profile <name>" variant
            for section in [profile_name, f"profile {profile_name}"]:
                if parser.has_section(section) and parser.has_option(section, "aws_access_key_id"):
                    return ResolvedCredential(
                        kind="api_key",
                        api_key=parser.get(section, "aws_access_key_id"),
                        metadata={
                            "secret_access_key": parser.get(
                                section, "aws_secret_access_key", fallback=""
                            ),
                            "session_token": parser.get(section, "aws_session_token", fallback=""),
                            "region": parser.get(section, "region", fallback=""),
                        },
                    )

        from nexus.bricks.auth.credential_backend import CredentialResolutionError

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"AWS profile '{profile_name}' not found in config files",
        )

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous resolve — re-reads config files (same logic as async)."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            parser = configparser.ConfigParser()
            parser.read_string(content)

            for section in [profile_name, f"profile {profile_name}"]:
                if parser.has_section(section) and parser.has_option(section, "aws_access_key_id"):
                    return ResolvedCredential(
                        kind="api_key",
                        api_key=parser.get(section, "aws_access_key_id"),
                        metadata={
                            "secret_access_key": parser.get(
                                section, "aws_secret_access_key", fallback=""
                            ),
                            "session_token": parser.get(section, "aws_session_token", fallback=""),
                            "region": parser.get(section, "region", fallback=""),
                        },
                    )

        from nexus.bricks.auth.credential_backend import CredentialResolutionError

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"AWS profile '{profile_name}' not found in config files",
        )
