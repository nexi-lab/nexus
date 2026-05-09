import pytest

from nexus.bricks.agent_log.brick import AgentLogBrick


async def _noop_mount(*, path, backend):
    return None


@pytest.mark.asyncio
async def test_brick_registers_mount_at_dot_activity():
    fake_mount_calls = []

    async def fake_add_mount(*, path, backend):
        fake_mount_calls.append((path, backend))

    class _DummyStore:  # MemoryBackend stand-in
        pass

    brick = AgentLogBrick(
        add_mount=fake_add_mount,
        add_rebac_grant=lambda **_: None,
        store=_DummyStore(),
    )
    await brick.startup(agent_ids=["alice", "bob"])

    assert len(fake_mount_calls) == 1
    assert fake_mount_calls[0][0] == "/.activity/"


@pytest.mark.asyncio
async def test_brick_grants_each_agent_read_on_their_own_log():
    grants = []

    def fake_grant(*, subject, relation, object):  # noqa: A002
        grants.append((subject, relation, object))

    brick = AgentLogBrick(add_mount=_noop_mount, add_rebac_grant=fake_grant, store=object())
    await brick.startup(agent_ids=["alice", "bob"])

    assert ("agent:alice", "can-read", "path:/.activity/*/alice.jsonl") in grants
    assert ("agent:bob", "can-read", "path:/.activity/*/bob.jsonl") in grants


@pytest.mark.asyncio
async def test_brick_on_agent_created_adds_grant():
    grants = []

    def fake_grant(*, subject, relation, object):  # noqa: A002
        grants.append((subject, relation, object))

    brick = AgentLogBrick(add_mount=_noop_mount, add_rebac_grant=fake_grant, store=object())
    await brick.startup(agent_ids=[])
    brick.on_agent_created("carol")
    assert ("agent:carol", "can-read", "path:/.activity/*/carol.jsonl") in grants


@pytest.mark.asyncio
async def test_brick_skips_mount_when_store_is_none():
    """If activity service is disabled, store is None; mount registration skipped."""
    fake_mount_calls = []

    async def fake_add_mount(*, path, backend):
        fake_mount_calls.append((path, backend))

    brick = AgentLogBrick(
        add_mount=fake_add_mount,
        add_rebac_grant=lambda **_: None,
        store=None,
    )
    await brick.startup(agent_ids=["alice"])
    assert fake_mount_calls == []
