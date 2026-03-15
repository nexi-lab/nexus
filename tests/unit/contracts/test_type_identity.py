"""Import path identity tests for contracts/ type promotions (Issue #2190).

Verifies that types imported from canonical (contracts/) and legacy (shim)
paths resolve to the **same Python object** — ensuring isinstance checks,
dict keys, pickling, etc. all work identically regardless of import path.
"""


class TestReBACTypesIdentity:
    """Verify nexus.contracts.rebac_types ↔ nexus.bricks.rebac.* identity."""

    def test_entity_identity(self) -> None:
        from nexus.bricks.rebac.domain import Entity as shim
        from nexus.contracts.rebac_types import Entity as canonical

        assert canonical is shim

    def test_wildcard_subject_identity(self) -> None:
        from nexus.bricks.rebac.domain import WILDCARD_SUBJECT as shim
        from nexus.contracts.rebac_types import WILDCARD_SUBJECT as canonical

        assert canonical is shim
