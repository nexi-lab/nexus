"""Zone settings Pydantic model for extensible zone configuration.

Issue #1180: Consistency Migration
Provides a Pydantic BaseModel with extra='allow' for forward-compatible
zone settings stored as JSON in ZoneModel.settings.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class SigningMode(StrEnum):
    """Message signing enforcement mode for a zone.

    - OFF: No signing, no verification.
    - VERIFY_ONLY: Verify signatures when present, warn if missing, allow through.
    - ENFORCE: Require valid signatures; reject unsigned or invalid messages.
    """

    OFF = "off"
    VERIFY_ONLY = "verify_only"
    ENFORCE = "enforce"


class ZoneSettings(BaseModel):
    """Extensible zone settings parsed from ZoneModel.settings JSON.

    Uses extra='allow' so that arbitrary keys are preserved without
    requiring schema changes. This enables forward compatibility --
    new settings can be added without migration.
    """

    model_config = {"extra": "allow"}

    message_signing: SigningMode = SigningMode.OFF
