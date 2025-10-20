"""Unit tests for skill audit logging."""

from datetime import datetime, timedelta

import pytest

from nexus.skills.audit import AuditAction, AuditLogEntry, SkillAuditLogger


@pytest.mark.asyncio
async def test_log_audit_entry() -> None:
    """Test logging an audit entry."""
    audit = SkillAuditLogger()

    audit_id = await audit.log(
        "test-skill",
        AuditAction.EXECUTED,
        agent_id="alice",
        details={"execution_time": 1.5, "success": True},
    )

    assert audit_id is not None
    assert len(audit._in_memory_logs) == 1

    entry = audit._in_memory_logs[0]
    assert entry.skill_name == "test-skill"
    assert entry.action == AuditAction.EXECUTED
    assert entry.agent_id == "alice"
    assert entry.details == {"execution_time": 1.5, "success": True}


@pytest.mark.asyncio
async def test_log_different_actions() -> None:
    """Test logging different audit actions."""
    audit = SkillAuditLogger()

    # Log different actions
    await audit.log("skill1", AuditAction.CREATED, agent_id="alice")
    await audit.log("skill1", AuditAction.FORKED, agent_id="bob", details={"parent": "skill1"})
    await audit.log("skill1", AuditAction.PUBLISHED, agent_id="alice")
    await audit.log("skill2", AuditAction.EXECUTED, agent_id="charlie")

    assert len(audit._in_memory_logs) == 4

    # Verify action types
    actions = [entry.action for entry in audit._in_memory_logs]
    assert AuditAction.CREATED in actions
    assert AuditAction.FORKED in actions
    assert AuditAction.PUBLISHED in actions
    assert AuditAction.EXECUTED in actions


@pytest.mark.asyncio
async def test_query_logs_by_skill() -> None:
    """Test querying logs by skill name."""
    audit = SkillAuditLogger()

    # Log for different skills
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="alice")
    await audit.log("skill2", AuditAction.EXECUTED, agent_id="bob")
    await audit.log("skill1", AuditAction.FORKED, agent_id="charlie")

    # Query for skill1
    logs = await audit.query_logs(skill_name="skill1")

    assert len(logs) == 2
    assert all(log.skill_name == "skill1" for log in logs)


@pytest.mark.asyncio
async def test_query_logs_by_action() -> None:
    """Test querying logs by action type."""
    audit = SkillAuditLogger()

    # Log different actions
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="alice")
    await audit.log("skill2", AuditAction.FORKED, agent_id="bob")
    await audit.log("skill3", AuditAction.EXECUTED, agent_id="charlie")

    # Query for EXECUTED actions
    logs = await audit.query_logs(action=AuditAction.EXECUTED)

    assert len(logs) == 2
    assert all(log.action == AuditAction.EXECUTED for log in logs)


@pytest.mark.asyncio
async def test_query_logs_by_agent() -> None:
    """Test querying logs by agent ID."""
    audit = SkillAuditLogger()

    # Log for different agents
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="alice")
    await audit.log("skill2", AuditAction.EXECUTED, agent_id="bob")
    await audit.log("skill3", AuditAction.FORKED, agent_id="alice")

    # Query for alice
    logs = await audit.query_logs(agent_id="alice")

    assert len(logs) == 2
    assert all(log.agent_id == "alice" for log in logs)


@pytest.mark.asyncio
async def test_query_logs_by_time_range() -> None:
    """Test querying logs by time range."""
    audit = SkillAuditLogger()

    # Log at different times (simulate)
    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    # Manually create entries with specific timestamps for testing
    from nexus.skills.audit import AuditLogEntry

    entry1 = AuditLogEntry(
        audit_id="1",
        skill_name="skill1",
        action=AuditAction.EXECUTED,
        agent_id="alice",
        tenant_id=None,
        details=None,
        timestamp=two_days_ago,
    )
    audit._in_memory_logs.append(entry1)

    entry2 = AuditLogEntry(
        audit_id="2",
        skill_name="skill2",
        action=AuditAction.EXECUTED,
        agent_id="bob",
        tenant_id=None,
        details=None,
        timestamp=yesterday,
    )
    audit._in_memory_logs.append(entry2)

    entry3 = AuditLogEntry(
        audit_id="3",
        skill_name="skill3",
        action=AuditAction.EXECUTED,
        agent_id="charlie",
        tenant_id=None,
        details=None,
        timestamp=now,
    )
    audit._in_memory_logs.append(entry3)

    # Query for logs since yesterday
    logs = await audit.query_logs(start_time=yesterday - timedelta(hours=1))

    assert len(logs) == 2  # entry2 and entry3


@pytest.mark.asyncio
async def test_query_logs_with_limit() -> None:
    """Test querying logs with result limit."""
    audit = SkillAuditLogger()

    # Log multiple entries
    for i in range(20):
        await audit.log(f"skill{i}", AuditAction.EXECUTED, agent_id="alice")

    # Query with limit
    logs = await audit.query_logs(limit=5)

    assert len(logs) == 5


@pytest.mark.asyncio
async def test_query_logs_multiple_filters() -> None:
    """Test querying logs with multiple filters."""
    audit = SkillAuditLogger()

    # Log various entries
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="alice", tenant_id="tenant1")
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="bob", tenant_id="tenant1")
    await audit.log("skill1", AuditAction.FORKED, agent_id="alice", tenant_id="tenant1")
    await audit.log("skill2", AuditAction.EXECUTED, agent_id="alice", tenant_id="tenant2")

    # Query with multiple filters
    logs = await audit.query_logs(
        skill_name="skill1",
        action=AuditAction.EXECUTED,
        agent_id="alice",
        tenant_id="tenant1",
    )

    assert len(logs) == 1
    assert logs[0].skill_name == "skill1"
    assert logs[0].action == AuditAction.EXECUTED
    assert logs[0].agent_id == "alice"
    assert logs[0].tenant_id == "tenant1"


@pytest.mark.asyncio
async def test_get_skill_activity() -> None:
    """Test getting activity summary for a skill."""
    audit = SkillAuditLogger()

    # Log various activities for a skill
    await audit.log("test-skill", AuditAction.CREATED, agent_id="alice")
    await audit.log("test-skill", AuditAction.EXECUTED, agent_id="bob")
    await audit.log("test-skill", AuditAction.EXECUTED, agent_id="charlie")
    await audit.log("test-skill", AuditAction.FORKED, agent_id="alice")
    await audit.log("other-skill", AuditAction.EXECUTED, agent_id="alice")

    # Get activity summary
    activity = await audit.get_skill_activity("test-skill")

    assert activity["skill_name"] == "test-skill"
    assert activity["total_logs"] == 4
    assert activity["total_executions"] == 2
    assert activity["unique_users"] == 3
    assert activity["last_activity"] is not None

    # Check action counts
    assert activity["action_counts"]["executed"] == 2
    assert activity["action_counts"]["created"] == 1
    assert activity["action_counts"]["forked"] == 1


@pytest.mark.asyncio
async def test_generate_compliance_report() -> None:
    """Test generating a compliance report."""
    audit = SkillAuditLogger()

    # Log various activities
    await audit.log("skill1", AuditAction.EXECUTED, agent_id="alice", tenant_id="tenant1")
    await audit.log("skill2", AuditAction.EXECUTED, agent_id="bob", tenant_id="tenant1")
    await audit.log("skill1", AuditAction.FORKED, agent_id="charlie", tenant_id="tenant1")
    await audit.log("skill3", AuditAction.CREATED, agent_id="alice", tenant_id="tenant1")

    # Generate report
    report = await audit.generate_compliance_report(tenant_id="tenant1")

    assert report["tenant_id"] == "tenant1"
    assert report["total_operations"] == 4
    assert report["skills_used"] == 3
    assert report["active_agents"] == 3

    # Check action counts
    assert report["action_counts"]["executed"] == 2
    assert report["action_counts"]["forked"] == 1
    assert report["action_counts"]["created"] == 1

    # Check top skills
    assert len(report["top_skills"]) > 0
    assert report["top_skills"][0] == ("skill1", 2)

    # Check recent activity
    assert len(report["recent_activity"]) == 4


@pytest.mark.asyncio
async def test_generate_compliance_report_with_time_range() -> None:
    """Test generating compliance report with time range filter."""
    audit = SkillAuditLogger()

    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)

    # Create entries with specific timestamps
    from nexus.skills.audit import AuditLogEntry

    old_entry = AuditLogEntry(
        audit_id="1",
        skill_name="skill1",
        action=AuditAction.EXECUTED,
        agent_id="alice",
        tenant_id="tenant1",
        details=None,
        timestamp=yesterday - timedelta(days=1),
    )
    audit._in_memory_logs.append(old_entry)

    recent_entry = AuditLogEntry(
        audit_id="2",
        skill_name="skill2",
        action=AuditAction.EXECUTED,
        agent_id="bob",
        tenant_id="tenant1",
        details=None,
        timestamp=now,
    )
    audit._in_memory_logs.append(recent_entry)

    # Generate report for last 24 hours
    report = await audit.generate_compliance_report(tenant_id="tenant1", start_time=yesterday)

    assert report["total_operations"] == 1  # Only recent_entry
    assert report["skills_used"] == 1


@pytest.mark.asyncio
async def test_audit_log_entry_validation() -> None:
    """Test audit log entry validation."""
    from datetime import datetime

    from nexus.core.exceptions import ValidationError

    # Valid entry
    entry = AuditLogEntry(
        audit_id="123",
        skill_name="test-skill",
        action=AuditAction.EXECUTED,
        agent_id="alice",
        tenant_id=None,
        details=None,
        timestamp=datetime.utcnow(),
    )
    entry.validate()  # Should not raise

    # Invalid: missing skill_name
    entry.skill_name = ""
    with pytest.raises(ValidationError, match="skill_name is required"):
        entry.validate()
