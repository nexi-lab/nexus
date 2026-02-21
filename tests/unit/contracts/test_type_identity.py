"""Import path identity tests for contracts/ type promotions (Issue #2190).

Verifies that types imported from canonical (contracts/) and legacy (shim)
paths resolve to the **same Python object** — ensuring isinstance checks,
dict keys, pickling, etc. all work identically regardless of import path.
"""


class TestLLMTypesIdentity:
    """Verify nexus.contracts.llm_types ↔ nexus.llm.message identity."""

    def test_message_identity(self) -> None:
        from nexus.contracts.llm_types import Message as canonical
        from nexus.llm.message import Message as shim

        assert canonical is shim

    def test_message_role_identity(self) -> None:
        from nexus.contracts.llm_types import MessageRole as canonical
        from nexus.llm.message import MessageRole as shim

        assert canonical is shim

    def test_content_type_identity(self) -> None:
        from nexus.contracts.llm_types import ContentType as canonical
        from nexus.llm.message import ContentType as shim

        assert canonical is shim

    def test_text_content_identity(self) -> None:
        from nexus.contracts.llm_types import TextContent as canonical
        from nexus.llm.message import TextContent as shim

        assert canonical is shim

    def test_image_content_identity(self) -> None:
        from nexus.contracts.llm_types import ImageContent as canonical
        from nexus.llm.message import ImageContent as shim

        assert canonical is shim

    def test_image_detail_identity(self) -> None:
        from nexus.contracts.llm_types import ImageDetail as canonical
        from nexus.llm.message import ImageDetail as shim

        assert canonical is shim


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

    def test_consistency_level_identity(self) -> None:
        from nexus.bricks.rebac.types import ConsistencyLevel as shim
        from nexus.contracts.rebac_types import ConsistencyLevel as canonical

        assert canonical is shim

    def test_graph_limits_identity(self) -> None:
        from nexus.bricks.rebac.types import GraphLimits as shim
        from nexus.contracts.rebac_types import GraphLimits as canonical

        assert canonical is shim

    def test_traversal_stats_identity(self) -> None:
        from nexus.bricks.rebac.types import TraversalStats as shim
        from nexus.contracts.rebac_types import TraversalStats as canonical

        assert canonical is shim

    def test_cross_zone_allowed_relations_identity(self) -> None:
        from nexus.bricks.rebac.cross_zone import CROSS_ZONE_ALLOWED_RELATIONS as shim
        from nexus.contracts.rebac_types import CROSS_ZONE_ALLOWED_RELATIONS as canonical

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
