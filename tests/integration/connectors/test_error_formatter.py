"""Dedicated tests for ReadmeErrorFormatter.

Written BEFORE merging format_error_with_skill_ref + format_trait_error
(Issue 6A) to lock down current behavior.
"""

import pytest

from nexus.backends.connectors.base import ErrorDef, ValidationError
from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter


@pytest.fixture()
def formatter() -> ReadmeErrorFormatter:
    return ReadmeErrorFormatter(skill_name="test_skill", mount_path="/mnt/test")


@pytest.fixture()
def registry() -> dict[str, ErrorDef]:
    return {
        "KNOWN_ERROR": ErrorDef(
            message="Something went wrong",
            readme_section="error-handling",
            fix_example="# fix: do this instead",
        ),
        "NO_FIX_ERROR": ErrorDef(
            message="No fix available",
            readme_section="errors",
        ),
    }


class TestSkillMdPath:
    def test_with_mount_path(self, formatter: ReadmeErrorFormatter) -> None:
        assert formatter.readme_md_path == "/mnt/test/.readme/README.md"

    def test_without_mount_path(self) -> None:
        f = ReadmeErrorFormatter(skill_name="test", mount_path="")
        assert f.readme_md_path == "/.readme/README.md"


class TestFormatErrorWithSkillRef:
    def test_known_error_from_registry(
        self,
        formatter: ReadmeErrorFormatter,
        registry: dict[str, ErrorDef],
    ) -> None:
        err = formatter.format_error_with_skill_ref(
            code="KNOWN_ERROR",
            message="",
            error_registry=registry,
        )
        assert isinstance(err, ValidationError)
        assert err.code == "KNOWN_ERROR"
        assert err.readme_section == "error-handling"
        assert err.fix_example == "# fix: do this instead"
        # Registry message should be used when message arg is empty
        assert "Something went wrong" in err.message

    def test_custom_message_overrides_registry(
        self,
        formatter: ReadmeErrorFormatter,
        registry: dict[str, ErrorDef],
    ) -> None:
        err = formatter.format_error_with_skill_ref(
            code="KNOWN_ERROR",
            message="Custom message",
            error_registry=registry,
        )
        assert "Custom message" in err.message

    def test_unknown_code_uses_params(self, formatter: ReadmeErrorFormatter) -> None:
        err = formatter.format_error_with_skill_ref(
            code="UNKNOWN",
            message="Fallback message",
            section="custom-section",
            fix_example="# custom fix",
        )
        assert err.code == "UNKNOWN"
        assert err.readme_section == "custom-section"
        assert err.fix_example == "# custom fix"

    def test_no_registry(self, formatter: ReadmeErrorFormatter) -> None:
        err = formatter.format_error_with_skill_ref(
            code="ANY_CODE",
            message="Direct message",
        )
        assert err.code == "ANY_CODE"
        assert "Direct message" in err.message


class TestFormatValidationError:
    def test_formats_field_errors(self, formatter: ReadmeErrorFormatter) -> None:
        err = formatter.format_validation_error(
            operation="create_event",
            field_errors={"summary": "field required", "start": "invalid format"},
        )
        assert err.code == "SCHEMA_VALIDATION_ERROR"
        assert err.readme_section == "create-event"
        assert "summary" in err.field_errors
        assert "start" in err.field_errors


class TestFormatTraitError:
    def test_known_error_from_registry(
        self,
        formatter: ReadmeErrorFormatter,
        registry: dict[str, ErrorDef],
    ) -> None:
        err = formatter.format_trait_error(
            code="KNOWN_ERROR",
            message="Trait failed",
            section="traits",
            fix="# default fix",
            error_registry=registry,
        )
        assert err.code == "KNOWN_ERROR"
        # Registry fix_example should override the default fix
        assert err.fix_example == "# fix: do this instead"
        # Registry readme_section should override the default section
        assert err.readme_section == "error-handling"

    def test_unknown_code_uses_defaults(self, formatter: ReadmeErrorFormatter) -> None:
        err = formatter.format_trait_error(
            code="UNKNOWN",
            message="Trait failed",
            section="traits",
            fix="# default fix",
        )
        assert err.code == "UNKNOWN"
        assert err.fix_example == "# default fix"
        assert err.readme_section == "traits"
