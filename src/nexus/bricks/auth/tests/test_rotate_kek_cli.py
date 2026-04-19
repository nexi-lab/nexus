"""Tests for `nexus auth rotate-kek` (issue #3803)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("click")

from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.cli_commands import auth
from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.tests.conftest import make_profile

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    try:
        eng = create_engine(PG_URL)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason="PostgreSQL not reachable at TEST_POSTGRES_URL.",
    ),
]


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    engine = create_engine(PG_URL, future=True)
    drop_schema(engine)
    ensure_schema(engine)
    yield engine
    drop_schema(engine)
    engine.dispose()


@pytest.fixture()
def seeded_tenant(
    pg_engine: Engine,
) -> Generator[tuple[uuid.UUID, InMemoryEncryptionProvider], None, None]:
    """Return (tenant_id, encryption_provider) with 2 rows at v1, provider at v2."""
    t = ensure_tenant(pg_engine, f"rot-{uuid.uuid4()}")
    p = ensure_principal(pg_engine, tenant_id=t, external_sub=f"s-{uuid.uuid4()}", auth_method="t")
    prov = InMemoryEncryptionProvider()
    store = PostgresAuthProfileStore(
        PG_URL, tenant_id=t, principal_id=p, engine=pg_engine, encryption_provider=prov
    )
    try:
        store.upsert_with_credential(
            make_profile("a"), ResolvedCredential(kind="api_key", api_key="k-a")
        )
        store.upsert_with_credential(
            make_profile("b"), ResolvedCredential(kind="api_key", api_key="k-b")
        )
    finally:
        store.close()
    prov.rotate()
    yield t, prov


def _tenant_name(engine: Engine, tenant_id: uuid.UUID) -> str:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT name FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        ).fetchone()
    assert row is not None
    return str(row[0])


class TestRotateKekCLI:
    def test_dry_run_reports_counts_no_writes(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "dry-run" in result.output.lower()
            assert "2" in result.output  # 2 stale rows
            # No writes: rows are still at v1
            with pg_engine.begin() as conn:
                conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t)})
                versions = sorted(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT kek_version FROM auth_profiles "
                            "WHERE tenant_id = :tid AND ciphertext IS NOT NULL"
                        ),
                        {"tid": t},
                    ).fetchall()
                )
            assert versions == [1, 1]
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_apply_with_provider_vault_invokes_builder(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--provider vault constructs a VaultTransitProvider via the builder.

        Regression for Codex round 2: the production wiring branches were
        never exercised by tests, so option plumbing bugs could ship.
        """
        t, prov = seeded_tenant
        called: dict[str, object] = {}

        def fake_builder(
            *, vault_addr, vault_token, vault_key, vault_mount
        ) -> InMemoryEncryptionProvider:
            called["vault_addr"] = vault_addr
            called["vault_token"] = vault_token
            called["vault_key"] = vault_key
            called["vault_mount"] = vault_mount
            return prov

        monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_vault_provider", fake_builder)

        runner = CliRunner()
        result = runner.invoke(
            auth,
            [
                "rotate-kek",
                "--db-url",
                PG_URL,
                "--tenant",
                _tenant_name(pg_engine, t),
                "--provider",
                "vault",
                "--vault-addr",
                "http://vault.example:8200",
                "--vault-token",
                "s.dummy",
                "--vault-key",
                "nexus",
                "--apply",
            ],
        )
        assert result.exit_code == 0, result.output
        assert called["vault_addr"] == "http://vault.example:8200"
        assert called["vault_token"] == "s.dummy"
        assert called["vault_key"] == "nexus"
        assert called["vault_mount"] == "transit"

    def test_apply_with_provider_aws_kms_invokes_builder(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--provider aws-kms constructs an AwsKmsProvider via the builder."""
        t, prov = seeded_tenant
        called: dict[str, object] = {}

        def fake_builder(
            *, kms_key_id, kms_region, kms_config_version
        ) -> InMemoryEncryptionProvider:
            called["kms_key_id"] = kms_key_id
            called["kms_region"] = kms_region
            called["kms_config_version"] = kms_config_version
            return prov

        monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_kms_provider", fake_builder)

        runner = CliRunner()
        result = runner.invoke(
            auth,
            [
                "rotate-kek",
                "--db-url",
                PG_URL,
                "--tenant",
                _tenant_name(pg_engine, t),
                "--provider",
                "aws-kms",
                "--kms-key-id",
                "arn:aws:kms:us-east-1:000000000000:key/abc",
                "--kms-region",
                "us-east-1",
                "--kms-config-version",
                "3",
                "--apply",
            ],
        )
        assert result.exit_code == 0, result.output
        kms_key_id = called["kms_key_id"]
        assert isinstance(kms_key_id, str) and kms_key_id.startswith("arn:aws:kms:")
        assert called["kms_region"] == "us-east-1"
        assert called["kms_config_version"] == 3

    def test_rejects_non_positive_batch_size(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        """--batch-size must be >= 1 (click.IntRange)."""
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                    "--batch-size",
                    "0",
                    "--apply",
                ],
            )
            assert result.exit_code != 0
            assert "batch-size" in result.output.lower() or "0" in result.output
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_preflight_fails_when_envelope_columns_missing(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        """Rotation does not run DDL; it only reads information_schema.

        Regression for Codex round 3: operators should get a clear migration-
        required error instead of rotate-kek silently running ALTER TABLE
        under a potentially under-privileged DB role.
        """
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            with pg_engine.begin() as conn:
                conn.execute(text("ALTER TABLE auth_profiles DROP COLUMN IF EXISTS kek_version"))
            try:
                runner = CliRunner()
                result = runner.invoke(
                    auth,
                    [
                        "rotate-kek",
                        "--db-url",
                        PG_URL,
                        "--tenant",
                        _tenant_name(pg_engine, t),
                    ],
                )
                assert result.exit_code != 0
                assert "kek_version" in result.output or "envelope columns" in result.output
            finally:
                with pg_engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS kek_version INTEGER"
                        )
                    )
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_rejects_non_positive_max_rows(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        """--max-rows must be >= 1 (click.IntRange)."""
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                    "--max-rows",
                    "-5",
                    "--apply",
                ],
            )
            assert result.exit_code != 0
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_tenant_id_flag_bypasses_name_lookup(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,  # noqa: ARG002 — fixture triggers schema+seed
    ) -> None:
        """--tenant-id skips the tenants.name SELECT (which FORCE RLS can block
        for least-privilege roles).
        """
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant-id",
                    str(t),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "dry-run" in result.output.lower()
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_requires_exactly_one_tenant_identifier(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,  # noqa: ARG002
    ) -> None:
        """Exactly one of --tenant / --tenant-id must be supplied."""
        _t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(auth, ["rotate-kek", "--db-url", PG_URL])
            assert result.exit_code != 0
            assert "exactly one" in result.output.lower()

            result2 = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    "x",
                    "--tenant-id",
                    "00000000-0000-0000-0000-000000000001",
                ],
            )
            assert result2.exit_code != 0
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_apply_exits_nonzero_on_failed_rows(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,  # noqa: ARG002
    ) -> None:
        """rows_failed > 0 must produce exit code 1 unless --allow-failures."""
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        t, real = seeded_tenant

        # Flaky provider that always fails unwrap (turns every row into failure)
        class _AllFailProvider:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):  # noqa: ARG002
                from nexus.bricks.auth.envelope import WrappedDEKInvalid

                raise WrappedDEKInvalid.from_row(
                    tenant_id=tenant_id,
                    profile_id="x",
                    kek_version=kek_version,
                    cause="forced-failure",
                )

        _TEST_PROVIDER_REGISTRY["allfail"] = _AllFailProvider
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "allfail"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant-id",
                    str(t),
                    "--apply",
                ],
            )
            assert result.exit_code == 1, result.output
            assert "failed" in result.output.lower()

            # With --allow-failures, exit 0 even with failed rows
            # (Re-seed isn't needed; same rows are still at v1)
            result2 = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant-id",
                    str(t),
                    "--apply",
                    "--allow-failures",
                ],
            )
            assert result2.exit_code == 0, result2.output
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("allfail", None)

    def test_no_provider_and_no_env_errors(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        """Omitting --provider without the test env var must fail cleanly."""
        t, _ = seeded_tenant
        os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
        runner = CliRunner()
        result = runner.invoke(
            auth,
            [
                "rotate-kek",
                "--db-url",
                PG_URL,
                "--tenant",
                _tenant_name(pg_engine, t),
            ],
        )
        assert result.exit_code != 0
        assert "--provider is required" in result.output

    def test_apply_rewraps_all(
        self,
        seeded_tenant: tuple[uuid.UUID, InMemoryEncryptionProvider],
        pg_engine: Engine,
    ) -> None:
        t, prov = seeded_tenant
        from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

        _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            runner = CliRunner()
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                    "--apply",
                ],
            )
            assert result.exit_code == 0, result.output
            with pg_engine.begin() as conn:
                conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t)})
                versions = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT DISTINCT kek_version FROM auth_profiles "
                            "WHERE tenant_id = :tid AND ciphertext IS NOT NULL"
                        ),
                        {"tid": t},
                    ).fetchall()
                ]
            assert versions == [2]
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)
