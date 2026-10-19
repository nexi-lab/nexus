"""Publication authority tests use a real file-backed database and independent connections."""

from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, event, inspect, select, update

from nexus.contracts.product_agent_registry import API, COMMAND_ADAPTER, PrincipalRef, parse_command
from nexus.services.product_agent_registry.signing import key_id
from nexus.services.product_agent_registry.store import (
    ProductAgentRegistry,
    RegistryActor,
    RegistryError,
    digest,
    events,
    initialize_schema,
    principals,
    versions,
)

ADMIN = RegistryActor("user:admin", "zone-a", True)
MEMBER = RegistryActor("user:member", "zone-a", False)
REF = PrincipalRef(trust_domain="agents.example", agent_name="assistant")
DIGEST = "sha256:" + "a" * 64


def command(kind, **kwargs):
    return COMMAND_ADAPTER.validate_python(
        {
            "api_version": API,
            "request_id": str(uuid4()),
            "principal": REF.model_dump(),
            "kind": kind,
            **kwargs,
        }
    )


def publish(version="v1", **kwargs):
    return command(
        "PublishVersion",
        version=version,
        spec_ref={
            "api_version": "common.sudo.dev/v1",
            "kind": "ResourceRef",
            "zone_id": "zone-a",
            "path": "/agents/spec.json",
            "digest": DIGEST,
        },
        spec_digest=DIGEST,
        **kwargs,
    )


@pytest.fixture
def registry(tmp_path):
    postgres = os.environ.get("NEXUS_REGISTRY_TEST_DATABASE_URL")
    schema = "registry_test_" + uuid4().hex
    bootstrap = None
    if postgres:
        bootstrap = create_engine(postgres)
        with bootstrap.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(postgres, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'registry.db'}", connect_args={"timeout": 10}
        )
    try:
        initialize_schema(engine)
        service = ProductAgentRegistry(
            engine,
            Ed25519PrivateKey.generate(),
            {
                "agents.example": "zone-a",
                "other.example": "zone-b",
            },
            clock=lambda: 1000,
        )
        yield service
    finally:
        engine.dispose()
        if bootstrap:
            with bootstrap.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            bootstrap.dispose()


def register(service):
    return service.command(ADMIN, command("RegisterPrincipal", display_name="Assistant"))


def ready(service):
    register(service)
    service.command(ADMIN, publish())
    service.command(
        ADMIN,
        command(
            "DecideVersion", version="v1", decision="approved", reason="reviewed", expires_at=2000
        ),
    )
    service.command(
        ADMIN,
        command(
            "MoveChannel", channel="stable", version="v1", expected_revision=0, reason="release"
        ),
    )


def test_initialization_is_explicit_and_additive(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with pytest.raises(RegistryError, match="REGISTRY_SCHEMA_REQUIRED"):
        ProductAgentRegistry(engine, Ed25519PrivateKey.generate(), {"agents.example": "zone-a"})
    assert inspect(engine).get_table_names() == []
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE legacy_tasks (value TEXT)")
        connection.exec_driver_sql("INSERT INTO legacy_tasks VALUES ('retained')")
    initialize_schema(engine)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT value FROM legacy_tasks").scalar_one() == "retained"
        )
    engine.dispose()


def test_signed_metadata_persists_across_service_restart(registry):
    ready(registry)
    before = registry.resolve(MEMBER, REF)
    payload = base64.b64decode(before.version.payload_base64, validate=True)
    registry.key.public_key().verify(base64.b64decode(before.version.signature_base64), payload)
    assert json.loads(payload) == before.version.manifest.model_dump()
    assert digest(payload) == before.version.digest
    assert before.version.manifest.evidence == "signed_registration_only"
    restarted = ProductAgentRegistry(
        registry.engine, registry.key, registry.domain_zones, clock=registry.clock
    )
    assert restarted.resolve(MEMBER, REF) == before


def test_reading_does_not_publish_approve_or_move_channels(registry):
    register(registry)
    registry.command(ADMIN, publish())
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        page = registry.list_versions(MEMBER, REF)
        assert page.items[0].decision is None and page.channels == []
        with pytest.raises(RegistryError, match="AGENT_VERSION_NOT_APPROVED"):
            registry.resolve(MEMBER, REF, version="v1")
        with pytest.raises(RegistryError, match="AGENT_CHANNEL_UNAVAILABLE"):
            registry.resolve(MEMBER, REF)
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert set(statements) <= {"BEGIN", "SELECT"}


def test_idempotency_and_immutable_label(registry):
    request = command("RegisterPrincipal", display_name="Assistant")
    result = registry.command(ADMIN, request)
    assert registry.command(ADMIN, request) == result
    with pytest.raises(RegistryError, match="REGISTRY_IDEMPOTENCY_CONFLICT"):
        registry.command(ADMIN, request.model_copy(update={"display_name": "different"}))
    first = registry.command(ADMIN, publish()).version
    assert registry.command(ADMIN, publish()).version == first
    changed = publish().model_dump()
    changed["spec_ref"]["path"] = "/agents/other.json"
    with pytest.raises(RegistryError, match="IMMUTABLE_VERSION_CONFLICT"):
        registry.command(ADMIN, COMMAND_ADAPTER.validate_python(changed))


@pytest.mark.parametrize(
    "identity", [MEMBER, RegistryActor("other", "zone-b", True), RegistryActor("", "zone-a", True)]
)
def test_unauthorized_writers_cannot_create_principals(registry, identity):
    with pytest.raises(RegistryError):
        registry.command(identity, command("RegisterPrincipal", display_name="Assistant"))
    with registry.engine.connect() as connection:
        assert connection.execute(select(principals)).all() == []


def test_domain_and_resource_zone_isolation(registry):
    ready(registry)
    foreign = RegistryActor("user:other", "zone-b", True)
    with pytest.raises(RegistryError, match="REGISTRY_FORBIDDEN"):
        registry.list_versions(foreign, REF)
    other_ref = PrincipalRef(trust_domain="other.example", agent_name=REF.agent_name)
    registry.command(
        foreign,
        command("RegisterPrincipal", principal=other_ref.model_dump(), display_name="Other"),
    )
    assert registry.list_versions(foreign, other_ref).items == []
    data = publish("v2").model_dump()
    data["spec_ref"]["zone_id"] = "zone-b"
    with pytest.raises(RegistryError, match="REGISTRY_RESOURCE_ZONE_FORBIDDEN"):
        registry.command(ADMIN, COMMAND_ADAPTER.validate_python(data))


@pytest.mark.parametrize("decision", ["withdrawn", "revoked"])
def test_decisions_disable_channels_and_increment_cas_revision(registry, decision):
    ready(registry)
    registry.command(
        ADMIN, command("DecideVersion", version="v1", decision=decision, reason="blocked")
    )
    page = registry.list_versions(MEMBER, REF)
    assert not page.channels[0].enabled and page.channels[0].revision == 2
    with pytest.raises(RegistryError, match="AGENT_CHANNEL_UNAVAILABLE"):
        registry.resolve(MEMBER, REF)
    approve = command(
        "DecideVersion", version="v1", decision="approved", reason="reviewed", expires_at=2000
    )
    if decision == "revoked":
        with pytest.raises(RegistryError, match="AGENT_VERSION_REVOKED"):
            registry.command(ADMIN, approve)
    else:
        registry.command(ADMIN, approve)
        with pytest.raises(RegistryError, match="CHANNEL_REVISION_CONFLICT"):
            registry.command(
                ADMIN,
                command(
                    "MoveChannel",
                    channel="stable",
                    version="v1",
                    expected_revision=1,
                    reason="stale",
                ),
            )
        registry.command(
            ADMIN,
            command(
                "MoveChannel",
                channel="stable",
                version="v1",
                expected_revision=2,
                reason="restored",
            ),
        )
        assert registry.resolve(MEMBER, REF).channel.revision == 3


def test_expiry_and_principal_status_fail_closed(registry):
    ready(registry)
    registry.clock = lambda: 2000
    with pytest.raises(RegistryError, match="AGENT_APPROVAL_EXPIRED"):
        registry.resolve(MEMBER, REF, version="v1")
    registry.clock = lambda: 1000
    revision = registry.list_versions(MEMBER, REF).principal.revision
    result = registry.command(
        ADMIN,
        command(
            "SetPrincipalStatus", status="suspended", expected_revision=revision, reason="suspend"
        ),
    )
    with pytest.raises(RegistryError, match="AGENT_PRINCIPAL_SUSPENDED"):
        registry.resolve(MEMBER, REF, version="v1")
    result = registry.command(
        ADMIN,
        command(
            "SetPrincipalStatus",
            status="active",
            expected_revision=result.principal.revision,
            reason="resume",
        ),
    )
    with pytest.raises(RegistryError, match="AGENT_CHANNEL_UNAVAILABLE"):
        registry.resolve(MEMBER, REF)
    result = registry.command(
        ADMIN,
        command(
            "SetPrincipalStatus",
            status="revoked",
            expected_revision=result.principal.revision,
            reason="revoke",
        ),
    )
    with pytest.raises(RegistryError, match="AGENT_PRINCIPAL_REVOKED"):
        registry.command(
            ADMIN,
            command(
                "SetPrincipalStatus",
                status="active",
                expected_revision=result.principal.revision,
                reason="invalid",
            ),
        )


def test_failed_transaction_rolls_back_new_principal_and_audit(registry):
    def fail_audit(_connection, _cursor, statement, *_args):
        if statement.startswith("INSERT INTO product_agent_events"):
            raise RuntimeError("injected storage fault")

    event.listen(registry.engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="injected storage fault"):
            register(registry)
    finally:
        event.remove(registry.engine, "before_cursor_execute", fail_audit)
    with registry.engine.connect() as connection:
        assert connection.execute(select(principals)).all() == []
        assert connection.execute(select(events)).all() == []
    register(registry)


def test_concurrent_retries_and_channel_cas_have_one_winner(registry):
    request = command("RegisterPrincipal", display_name="Assistant")
    barrier = Barrier(2)

    def register_once(_index):
        barrier.wait(timeout=5)
        return registry.command(ADMIN, request)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(register_once, range(2)))
    assert responses[0] == responses[1]
    registry.command(ADMIN, publish())
    registry.command(
        ADMIN,
        command(
            "DecideVersion", version="v1", decision="approved", reason="reviewed", expires_at=2000
        ),
    )
    barrier = Barrier(2)

    def move_once(_index):
        barrier.wait(timeout=5)
        try:
            registry.command(
                ADMIN,
                command(
                    "MoveChannel",
                    channel="stable",
                    version="v1",
                    expected_revision=0,
                    reason="release",
                ),
            )
            return "ok"
        except RegistryError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(move_once, range(2))) == ["CHANNEL_REVISION_CONFLICT", "ok"]
    assert registry.resolve(MEMBER, REF).channel.revision == 1


@pytest.mark.parametrize("data", [b'{"kind":"a","kind":"b"}', b"{}" * 40000, b'{"__proto__":{}}'])
def test_invalid_command_bodies_are_rejected(data):
    with pytest.raises(ValueError):
        parse_command(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("signature_base64", "AAAA"),
        ("payload_base64", "{}"),
        ("digest", "sha256:" + "0" * 64),
        ("key_id", "sha256:" + "0" * 64),
        ("manifest", None),
    ],
)
def test_tampered_registration_is_never_resolved(registry, field, value):
    ready(registry)
    signed = registry.resolve(MEMBER, REF).version.model_dump()
    if field == "manifest":
        signed[field]["spec_ref"]["path"] = "/tampered.json"
    else:
        signed[field] = value
    with registry.engine.begin() as connection:
        connection.execute(update(versions).values(document=json.dumps(signed)))
    with pytest.raises(RegistryError, match="REGISTRY_SIGNATURE_INVALID"):
        registry.resolve(MEMBER, REF)
    with pytest.raises(RegistryError, match="REGISTRY_SIGNATURE_INVALID"):
        registry.list_versions(MEMBER, REF)


def test_rotation_requires_retained_trust_for_historical_versions(registry):
    ready(registry)
    replacement = Ed25519PrivateKey.generate()
    rotated = ProductAgentRegistry(
        registry.engine, replacement, registry.domain_zones, clock=registry.clock
    )
    with pytest.raises(RegistryError, match="REGISTRY_SIGNATURE_INVALID"):
        rotated.resolve(MEMBER, REF)
    old_public = registry.key.public_key()
    rotated = ProductAgentRegistry(
        registry.engine,
        replacement,
        registry.domain_zones,
        clock=registry.clock,
        trusted_keys={key_id(old_public): old_public},
    )
    assert rotated.resolve(MEMBER, REF).version.key_id == registry.key_id
    assert rotated.command(ADMIN, publish("v2")).version.key_id == rotated.key_id


@pytest.mark.parametrize(
    "domain_zones", [None, [], ["agents.example"], {}, {"agents.example": "../invalid"}]
)
def test_invalid_domain_configuration_is_rejected(registry, domain_zones):
    with pytest.raises(ValueError):
        ProductAgentRegistry(registry.engine, registry.key, domain_zones)


def test_lone_surrogates_are_rejected_before_persistence():
    data = command("RegisterPrincipal", display_name="Assistant").model_dump()
    data["display_name"] = "\ud800"
    with pytest.raises(ValueError):
        parse_command(json.dumps(data).encode())
