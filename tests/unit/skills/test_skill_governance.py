"""Unit tests for skill governance."""

import pytest

from nexus.skills.governance import (
    ApprovalStatus,
    GovernanceError,
    SkillApproval,
    SkillGovernance,
)


@pytest.mark.asyncio
async def test_submit_for_approval() -> None:
    """Test submitting a skill for approval."""
    gov = SkillGovernance()

    approval_id = await gov.submit_for_approval(
        "test-skill",
        submitted_by="alice",
        reviewers=["bob", "charlie"],
        comments="Ready for org-wide use",
    )

    assert approval_id is not None
    assert approval_id in gov._in_memory_approvals

    approval = gov._in_memory_approvals[approval_id]
    assert approval.skill_name == "test-skill"
    assert approval.submitted_by == "alice"
    assert approval.status == ApprovalStatus.PENDING
    assert approval.reviewers == ["bob", "charlie"]
    assert approval.comments == "Ready for org-wide use"


@pytest.mark.asyncio
async def test_submit_duplicate_approval() -> None:
    """Test submitting duplicate approval raises error."""
    gov = SkillGovernance()

    # First submission
    await gov.submit_for_approval("test-skill", submitted_by="alice")

    # Second submission should fail
    with pytest.raises(GovernanceError, match="already has a pending approval"):
        await gov.submit_for_approval("test-skill", submitted_by="bob")


@pytest.mark.asyncio
async def test_approve_skill() -> None:
    """Test approving a skill."""
    gov = SkillGovernance()

    # Submit for approval
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")

    # Approve
    await gov.approve_skill(approval_id, reviewed_by="bob", comments="Looks great!")

    approval = gov._in_memory_approvals[approval_id]
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.reviewed_by == "bob"
    assert approval.comments == "Looks great!"
    assert approval.reviewed_at is not None


@pytest.mark.asyncio
async def test_approve_nonexistent() -> None:
    """Test approving non-existent approval raises error."""
    gov = SkillGovernance()

    with pytest.raises(GovernanceError, match="Approval not found"):
        await gov.approve_skill("nonexistent", reviewed_by="bob")


@pytest.mark.asyncio
async def test_approve_already_approved() -> None:
    """Test approving already-approved skill raises error."""
    gov = SkillGovernance()

    # Submit and approve
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")
    await gov.approve_skill(approval_id, reviewed_by="bob")

    # Try to approve again
    with pytest.raises(GovernanceError, match="already approved"):
        await gov.approve_skill(approval_id, reviewed_by="charlie")


@pytest.mark.asyncio
async def test_reject_skill() -> None:
    """Test rejecting a skill."""
    gov = SkillGovernance()

    # Submit for approval
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")

    # Reject
    await gov.reject_skill(approval_id, reviewed_by="bob", comments="Needs more documentation")

    approval = gov._in_memory_approvals[approval_id]
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.reviewed_by == "bob"
    assert approval.comments == "Needs more documentation"
    assert approval.reviewed_at is not None


@pytest.mark.asyncio
async def test_reject_already_rejected() -> None:
    """Test rejecting already-rejected skill raises error."""
    gov = SkillGovernance()

    # Submit and reject
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")
    await gov.reject_skill(approval_id, reviewed_by="bob")

    # Try to reject again
    with pytest.raises(GovernanceError, match="already rejected"):
        await gov.reject_skill(approval_id, reviewed_by="charlie")


@pytest.mark.asyncio
async def test_is_approved() -> None:
    """Test checking if a skill is approved."""
    gov = SkillGovernance()

    # Not approved initially
    assert await gov.is_approved("test-skill") is False

    # Submit and approve
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")
    await gov.approve_skill(approval_id, reviewed_by="bob")

    # Should be approved now
    assert await gov.is_approved("test-skill") is True


@pytest.mark.asyncio
async def test_is_approved_after_rejection() -> None:
    """Test that rejected skill is not approved."""
    gov = SkillGovernance()

    # Submit and reject
    approval_id = await gov.submit_for_approval("test-skill", submitted_by="alice")
    await gov.reject_skill(approval_id, reviewed_by="bob")

    # Should not be approved
    assert await gov.is_approved("test-skill") is False


@pytest.mark.asyncio
async def test_get_pending_approvals() -> None:
    """Test getting pending approvals."""
    gov = SkillGovernance()

    # Submit multiple approvals
    await gov.submit_for_approval("skill1", submitted_by="alice", reviewers=["bob"])
    await gov.submit_for_approval("skill2", submitted_by="bob", reviewers=["charlie"])

    # Approve one
    approval_id = await gov.submit_for_approval("skill3", submitted_by="charlie")
    await gov.approve_skill(approval_id, reviewed_by="bob")

    # Get pending
    pending = await gov.get_pending_approvals()

    assert len(pending) == 2
    skill_names = [a.skill_name for a in pending]
    assert "skill1" in skill_names
    assert "skill2" in skill_names
    assert "skill3" not in skill_names  # Already approved


@pytest.mark.asyncio
async def test_get_pending_approvals_by_reviewer() -> None:
    """Test getting pending approvals filtered by reviewer."""
    gov = SkillGovernance()

    # Submit with different reviewers
    await gov.submit_for_approval("skill1", submitted_by="alice", reviewers=["bob"])
    await gov.submit_for_approval("skill2", submitted_by="bob", reviewers=["charlie"])
    await gov.submit_for_approval("skill3", submitted_by="alice", reviewers=["bob", "charlie"])

    # Get approvals for bob
    bob_approvals = await gov.get_pending_approvals(reviewer="bob")

    assert len(bob_approvals) == 2
    skill_names = [a.skill_name for a in bob_approvals]
    assert "skill1" in skill_names
    assert "skill3" in skill_names


@pytest.mark.asyncio
async def test_get_approval_history() -> None:
    """Test getting approval history for a skill."""
    import asyncio

    gov = SkillGovernance()

    # Submit, approve, then submit again (with delay to ensure different timestamps)
    approval_id1 = await gov.submit_for_approval("test-skill", submitted_by="alice")
    await gov.approve_skill(approval_id1, reviewed_by="bob")

    # Small delay to ensure different timestamps on fast systems
    await asyncio.sleep(0.01)

    approval_id2 = await gov.submit_for_approval("test-skill", submitted_by="charlie")
    await gov.reject_skill(approval_id2, reviewed_by="bob")

    # Get history
    history = await gov.get_approval_history("test-skill")

    assert len(history) == 2

    # Should be sorted by submission date (newest first)
    assert history[0].approval_id == approval_id2
    assert history[0].status == ApprovalStatus.REJECTED

    assert history[1].approval_id == approval_id1
    assert history[1].status == ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_approval_validation() -> None:
    """Test approval record validation."""
    from datetime import datetime

    from nexus.core.exceptions import ValidationError

    # Valid approval
    approval = SkillApproval(
        approval_id="123",
        skill_name="test-skill",
        submitted_by="alice",
        status=ApprovalStatus.PENDING,
        submitted_at=datetime.utcnow(),
    )
    approval.validate()  # Should not raise

    # Invalid: missing skill_name
    approval.skill_name = ""
    with pytest.raises(ValidationError, match="skill_name is required"):
        approval.validate()
