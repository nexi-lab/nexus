"""Zone settings Pydantic model for extensible zone configuration.

Issue #1180: Consistency Migration
Provides a Pydantic BaseModel with extra='allow' for forward-compatible
zone settings stored as JSON in ZoneModel.settings.
"""

from __future__ import annotations

from pydantic import BaseModel


class ZoneSettings(BaseModel):
    """Extensible zone settings parsed from ZoneModel.settings JSON.

    Uses extra='allow' so that arbitrary keys are preserved without
    requiring schema changes. This enables forward compatibility --
    new settings can be added without migration.
    """

    model_config = {"extra": "allow"}
