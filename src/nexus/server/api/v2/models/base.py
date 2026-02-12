"""Base model for all API v2 request/response types.

All v2 models inherit from ApiModel to enforce consistent
configuration across the API surface.

ConfigDict(extra="ignore") implements the Tolerant Reader pattern:
unknown fields in requests are silently ignored, enabling forward
compatibility when clients send fields added in newer versions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    """Base model for all API v2 request and response types.

    Enforces:
    - extra="ignore": Unknown fields are silently dropped (Tolerant Reader).
    """

    model_config = ConfigDict(extra="ignore")
