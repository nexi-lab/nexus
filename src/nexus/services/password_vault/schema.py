"""VaultEntry schema — the domain type stored by PasswordVaultService.

A ``VaultEntry`` represents a single logical credential. It is persisted
as a JSON string inside SecretsService (which stays opaque, dealing in
encrypted bytes). ``title`` is the stable identifier and maps directly
to the SecretsService key.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VaultEntry(BaseModel):
    """A single credential in the password vault.

    Only ``title`` is required. All other fields are optional so entries
    can be as small as ``{title, password}`` or carry a full profile
    (TOTP seed, tags, arbitrary extras such as bank PINs).

    Mirrors the Tolerant Reader pattern from ``server.api.v2.models.base``:
    unknown fields in requests are silently dropped for forward compat.
    The config is inlined rather than inherited from ``ApiModel`` to
    keep ``services/`` free of ``server/`` imports (kernel layering).
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1, description="Stable identifier; used as the storage key.")
    username: str | None = None
    password: str | None = None
    url: str | None = None
    notes: str | None = None
    tags: str | None = Field(default=None, description="Comma-separated tag list.")
    totp_secret: str | None = None
    extra: dict[str, Any] | None = Field(
        default=None,
        description="Arbitrary JSON for extra fields (e.g., bank PINs, recovery codes).",
    )
