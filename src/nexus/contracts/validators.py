"""Shared validators for the Nexus VFS (Issue #2085).

Tier-neutral validation types used across connectors, services, and bricks.
Zero imports from ``nexus.core`` or any other kernel module.

Usage:
    from nexus.contracts.validators import EmailAddress, EmailList, ISODateTimeStr

    class MySchema(BaseModel):
        to: EmailList
        start: ISODateTimeStr
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, EmailStr, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

# ISO 8601 datetime with required timezone offset.
# Matches: 2024-01-15T09:00:00Z, 2024-01-15T09:00:00-08:00, 2024-01-15T09:00:00+05:30
_ISO8601_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")

# Re-export for backward compatibility with tests referencing the raw pattern.
ISO8601_PATTERN = _ISO8601_PATTERN

# ---------------------------------------------------------------------------
# Email validators
# ---------------------------------------------------------------------------

# Single email address — uses Pydantic's EmailStr (RFC 5322 via email-validator).
EmailAddress = EmailStr

# Module-level TypeAdapter — built once, reused across all validations.
_EMAIL_ADAPTER: TypeAdapter[EmailStr] = TypeAdapter(EmailStr)


def _validate_email_list(v: list[str] | None) -> list[str] | None:
    """Validate and normalize a list of email addresses.

    Uses Pydantic's ``EmailStr`` validation for each address
    and lowercases the result.
    """
    if v is None:
        return None
    validated: list[str] = []
    for email in v:
        try:
            _EMAIL_ADAPTER.validate_python(email)
        except PydanticValidationError as exc:
            raise ValueError(f"Invalid email address: {email!r}") from exc
        validated.append(email.lower())
    return validated


def _validate_email_list_required(v: list[str]) -> list[str]:
    """Validate a required (non-optional) email list."""
    result = _validate_email_list(v)
    if result is None:
        raise ValueError("Email list is required")
    return result


# List of email addresses with validation and lowercasing (optional).
EmailList = Annotated[list[str] | None, BeforeValidator(_validate_email_list)]

# Non-optional email list (at least 1 recipient required).
EmailListRequired = Annotated[
    list[str],
    Field(min_length=1),
    BeforeValidator(_validate_email_list_required),
]


# ---------------------------------------------------------------------------
# Datetime validators
# ---------------------------------------------------------------------------


def _validate_iso8601(v: str) -> str:
    """Validate ISO 8601 datetime with timezone offset."""
    if not _ISO8601_PATTERN.match(v):
        raise ValueError(
            f"Invalid datetime format: {v}. "
            "Use ISO 8601 with timezone offset (e.g., 2024-01-15T09:00:00-08:00)"
        )
    return v


# ISO 8601 datetime string with required timezone offset.
ISODateTimeStr = Annotated[str, AfterValidator(_validate_iso8601)]
