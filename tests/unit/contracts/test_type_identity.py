"""Import path identity tests for contracts/ type promotions (Issue #2190).

Verifies that types imported from canonical (contracts/) and legacy (shim)
paths resolve to the **same Python object** — ensuring isinstance checks,
dict keys, pickling, etc. all work identically regardless of import path.
"""


class TestLLMTypesIdentity:
    """Verify nexus.contracts.llm_types ↔ nexus.bricks.llm identity."""

    def test_message_identity(self) -> None:
        from nexus.bricks.llm import Message as shim
        from nexus.contracts.llm_types import Message as canonical

        assert canonical is shim

    def test_message_role_identity(self) -> None:
        from nexus.bricks.llm import MessageRole as shim
        from nexus.contracts.llm_types import MessageRole as canonical

        assert canonical is shim

    def test_content_type_identity(self) -> None:
        from nexus.bricks.llm import ContentType as shim
        from nexus.contracts.llm_types import ContentType as canonical

        assert canonical is shim

    def test_text_content_identity(self) -> None:
        from nexus.bricks.llm import TextContent as shim
        from nexus.contracts.llm_types import TextContent as canonical

        assert canonical is shim

    def test_image_content_identity(self) -> None:
        from nexus.bricks.llm import ImageContent as shim
        from nexus.contracts.llm_types import ImageContent as canonical

        assert canonical is shim

    def test_image_detail_identity(self) -> None:
        from nexus.bricks.llm import ImageDetail as shim
        from nexus.contracts.llm_types import ImageDetail as canonical

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
