"""Unit tests for skill analytics."""

from datetime import datetime

import pytest

from nexus.skills.analytics import (
    SkillAnalytics,
    SkillAnalyticsTracker,
    SkillUsageRecord,
)


@pytest.mark.asyncio
async def test_track_usage() -> None:
    """Test tracking skill usage."""
    tracker = SkillAnalyticsTracker()

    usage_id = await tracker.track_usage(
        "test-skill",
        agent_id="alice",
        execution_time=1.5,
        success=True,
    )

    assert usage_id is not None
    assert len(tracker._in_memory_records) == 1

    record = tracker._in_memory_records[0]
    assert record.skill_name == "test-skill"
    assert record.agent_id == "alice"
    assert record.execution_time == 1.5
    assert record.success is True


@pytest.mark.asyncio
async def test_track_usage_failure() -> None:
    """Test tracking failed skill execution."""
    tracker = SkillAnalyticsTracker()

    usage_id = await tracker.track_usage(
        "test-skill",
        agent_id="bob",
        execution_time=0.5,
        success=False,
        error_message="Test error",
    )

    assert usage_id is not None

    record = tracker._in_memory_records[0]
    assert record.success is False
    assert record.error_message == "Test error"


@pytest.mark.asyncio
async def test_get_skill_analytics() -> None:
    """Test getting analytics for a skill."""
    tracker = SkillAnalyticsTracker()

    # Track multiple usages
    await tracker.track_usage("test-skill", agent_id="alice", execution_time=1.0, success=True)
    await tracker.track_usage("test-skill", agent_id="bob", execution_time=2.0, success=True)
    await tracker.track_usage("test-skill", agent_id="alice", execution_time=1.5, success=False)

    analytics = await tracker.get_skill_analytics("test-skill")

    assert analytics.skill_name == "test-skill"
    assert analytics.usage_count == 3
    assert analytics.success_count == 2
    assert analytics.failure_count == 1
    assert analytics.success_rate == pytest.approx(2 / 3)
    assert analytics.avg_execution_time == pytest.approx((1.0 + 2.0 + 1.5) / 3)
    assert analytics.unique_users == 2


@pytest.mark.asyncio
async def test_get_skill_analytics_empty() -> None:
    """Test getting analytics for non-existent skill."""
    tracker = SkillAnalyticsTracker()

    analytics = await tracker.get_skill_analytics("nonexistent")

    assert analytics.skill_name == "nonexistent"
    assert analytics.usage_count == 0
    assert analytics.success_rate == 0.0


@pytest.mark.asyncio
async def test_get_skill_analytics_tenant_filter() -> None:
    """Test getting analytics filtered by tenant."""
    tracker = SkillAnalyticsTracker()

    # Track usages for different tenants
    await tracker.track_usage("test-skill", tenant_id="tenant1", success=True)
    await tracker.track_usage("test-skill", tenant_id="tenant2", success=True)
    await tracker.track_usage("test-skill", tenant_id="tenant1", success=False)

    # Get analytics for tenant1
    analytics = await tracker.get_skill_analytics("test-skill", tenant_id="tenant1")

    assert analytics.usage_count == 2
    assert analytics.success_count == 1
    assert analytics.failure_count == 1


@pytest.mark.asyncio
async def test_get_dashboard_metrics() -> None:
    """Test getting organization-wide dashboard metrics."""
    tracker = SkillAnalyticsTracker()

    # Track usages for multiple skills and agents
    await tracker.track_usage("skill1", agent_id="alice", execution_time=1.0, success=True)
    await tracker.track_usage("skill1", agent_id="bob", execution_time=2.0, success=True)
    await tracker.track_usage("skill2", agent_id="alice", execution_time=1.5, success=False)
    await tracker.track_usage("skill3", agent_id="charlie", execution_time=3.0, success=True)

    metrics = await tracker.get_dashboard_metrics()

    assert metrics.total_skills == 3
    assert metrics.total_usage_count == 4
    assert metrics.total_users == 3

    # Most used skills
    assert len(metrics.most_used_skills) == 3
    assert metrics.most_used_skills[0] == ("skill1", 2)

    # Top contributors
    assert len(metrics.top_contributors) == 3
    assert ("alice", 2) in metrics.top_contributors

    # Success rates
    assert "skill1" in metrics.success_rates
    assert metrics.success_rates["skill1"] == 1.0
    assert metrics.success_rates["skill2"] == 0.0

    # Avg execution times
    assert "skill1" in metrics.avg_execution_times
    assert metrics.avg_execution_times["skill1"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_get_dashboard_metrics_tenant_filter() -> None:
    """Test dashboard metrics filtered by tenant."""
    tracker = SkillAnalyticsTracker()

    # Track usages for different tenants
    await tracker.track_usage("skill1", tenant_id="tenant1", agent_id="alice", success=True)
    await tracker.track_usage("skill1", tenant_id="tenant2", agent_id="bob", success=True)
    await tracker.track_usage("skill2", tenant_id="tenant1", agent_id="alice", success=True)

    # Get metrics for tenant1
    metrics = await tracker.get_dashboard_metrics(tenant_id="tenant1")

    assert metrics.total_skills == 2
    assert metrics.total_usage_count == 2
    assert metrics.total_users == 1


@pytest.mark.asyncio
async def test_usage_record_validation() -> None:
    """Test usage record validation."""
    from nexus.core.exceptions import ValidationError

    # Valid record
    record = SkillUsageRecord(
        usage_id="123",
        skill_name="test-skill",
        agent_id="alice",
        tenant_id=None,
        execution_time=1.0,
        success=True,
        error_message=None,
        timestamp=datetime.utcnow(),
    )
    record.validate()  # Should not raise

    # Invalid: negative execution time
    record.execution_time = -1.0
    with pytest.raises(ValidationError, match="execution_time cannot be negative"):
        record.validate()


@pytest.mark.asyncio
async def test_skill_analytics_calculate_success_rate() -> None:
    """Test calculating success rate."""
    analytics = SkillAnalytics(skill_name="test-skill", usage_count=10, success_count=7)

    analytics.calculate_success_rate()

    assert analytics.success_rate == pytest.approx(0.7)

    # Test with zero usage
    analytics2 = SkillAnalytics(skill_name="test-skill", usage_count=0, success_count=0)
    analytics2.calculate_success_rate()

    assert analytics2.success_rate == 0.0
