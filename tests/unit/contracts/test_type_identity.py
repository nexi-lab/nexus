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


class TestSearchTypesIdentity:
    """Verify nexus.contracts.search_types ↔ nexus.bricks.search.strategies identity."""

    def test_search_strategy_identity(self) -> None:
        from nexus.bricks.search.strategies import SearchStrategy as shim
        from nexus.contracts.search_types import SearchStrategy as canonical

        assert canonical is shim

    def test_glob_strategy_identity(self) -> None:
        from nexus.bricks.search.strategies import GlobStrategy as shim
        from nexus.contracts.search_types import GlobStrategy as canonical

        assert canonical is shim

    def test_grep_sequential_threshold_identity(self) -> None:
        from nexus.bricks.search.strategies import GREP_SEQUENTIAL_THRESHOLD as shim
        from nexus.contracts.search_types import GREP_SEQUENTIAL_THRESHOLD as canonical

        assert canonical == shim
