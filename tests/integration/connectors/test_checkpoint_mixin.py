"""Regression test for CheckpointMixin shared-dict bug (#7-A)."""

from nexus.backends.connectors.base import CheckpointMixin, OpTraits, Reversibility


class FakeCheckpointConnector(CheckpointMixin):
    OPERATION_TRAITS = {
        "create": OpTraits(reversibility=Reversibility.FULL, checkpoint=True),
    }

    def __init__(self) -> None:
        self._checkpoints = {}


class TestCheckpointsNotSharedAcrossInstances:
    def test_isolation(self) -> None:
        """Two CheckpointMixin instances must NOT share checkpoint state."""
        a = FakeCheckpointConnector()
        b = FakeCheckpointConnector()

        cp = a.create_checkpoint("create", previous_state={"id": "1"})
        assert cp is not None

        # Instance b must not see a's checkpoint
        assert b.get_checkpoint(cp.checkpoint_id) is None
        assert len(b._checkpoints) == 0
        assert len(a._checkpoints) == 1
