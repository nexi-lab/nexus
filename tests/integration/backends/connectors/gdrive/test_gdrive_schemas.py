"""Tests for Google Drive connector backend schemas and mixin integration.

Covers:
- PathGDriveBackend has SCHEMAS, OPERATION_TRAITS, SKILL_NAME
- README_DOC and WRITE_BACK capabilities are declared
- Mixin class hierarchy is correct
"""

from nexus.backends.connectors.base import (
    OpTraits,
    ReadmeDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.gdrive.connector import PathGDriveBackend
from nexus.backends.connectors.gws.schemas import (
    DeleteFileSchema,
    UpdateFileSchema,
    UploadFileSchema,
)
from nexus.contracts.backend_features import BackendFeature


class TestPathGDriveBackendMixins:
    """Test that the connector has correct mixin configuration."""

    def test_has_skill_name(self) -> None:
        assert PathGDriveBackend.SKILL_NAME == "gdrive"

    def test_has_schemas(self) -> None:
        schemas = PathGDriveBackend.SCHEMAS
        assert "upload_file" in schemas
        assert "update_file" in schemas
        assert "delete_file" in schemas
        assert schemas["upload_file"] is UploadFileSchema
        assert schemas["update_file"] is UpdateFileSchema
        assert schemas["delete_file"] is DeleteFileSchema

    def test_has_operation_traits(self) -> None:
        traits = PathGDriveBackend.OPERATION_TRAITS
        assert "upload_file" in traits
        assert "update_file" in traits
        assert "delete_file" in traits
        assert isinstance(traits["upload_file"], OpTraits)
        assert isinstance(traits["update_file"], OpTraits)
        assert isinstance(traits["delete_file"], OpTraits)

    def test_upload_traits(self) -> None:
        traits = PathGDriveBackend.OPERATION_TRAITS["upload_file"]
        assert traits.reversibility == "full"
        assert traits.confirm == "intent"
        assert traits.checkpoint is True

    def test_update_traits(self) -> None:
        traits = PathGDriveBackend.OPERATION_TRAITS["update_file"]
        assert traits.reversibility == "partial"
        assert traits.confirm == "explicit"

    def test_delete_traits(self) -> None:
        traits = PathGDriveBackend.OPERATION_TRAITS["delete_file"]
        assert traits.reversibility == "partial"
        assert traits.confirm == "user"

    def test_has_error_registry(self) -> None:
        registry = PathGDriveBackend.ERROR_REGISTRY
        assert "MISSING_AGENT_INTENT" in registry
        assert "MISSING_FILE_ID" in registry
        assert "MISSING_CONFIRM" in registry

    def test_readme_doc_capability(self) -> None:
        caps = PathGDriveBackend._BACKEND_FEATURES
        assert BackendFeature.README_DOC in caps

    def test_write_back_capability(self) -> None:
        caps = PathGDriveBackend._BACKEND_FEATURES
        assert BackendFeature.WRITE_BACK in caps

    def test_inherits_readme_doc_mixin(self) -> None:
        assert issubclass(PathGDriveBackend, ReadmeDocMixin)

    def test_inherits_validated_mixin(self) -> None:
        assert issubclass(PathGDriveBackend, ValidatedMixin)

    def test_inherits_trait_based_mixin(self) -> None:
        assert issubclass(PathGDriveBackend, TraitBasedMixin)
