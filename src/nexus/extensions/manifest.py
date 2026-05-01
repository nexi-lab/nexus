"""Pydantic discriminated-union manifest contract.

This module defines the data shape every extension (plugin, connector, brick)
declares in its sibling _manifest.py file. It must NOT import any
extension impl module — that boundary keeps introspection lazy.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from nexus.extensions.errors import ReservedNameError
from nexus.extensions.types import Kind


class RuntimeDep(BaseModel):
    """A dependency required to actually run an extension.

    Distinct from ``import_probes`` — runtime_deps are declarative and used to
    generate human-readable install hints; probes are best-effort module
    presence checks for ``nexus extensions check``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["python", "binary", "service"]
    name: str
    extras: tuple[str, ...] = ()
    install_hint: str | None = None


# Reserved name patterns. Matched in order; first hit wins for the error message.
_RESERVED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("empty", re.compile(r"^$")),
    ("leading underscore", re.compile(r"^_")),
    ("glob", re.compile(r"^\*$")),
    ("reserved nexus name", re.compile(r"^nexus$")),
    ("reserved nexus prefix", re.compile(r"^nexus-")),
)


def _validate_name(name: str) -> str:
    for label, pattern in _RESERVED_PATTERNS:
        if pattern.match(name):
            raise ReservedNameError(name=name, pattern=label)
    return name


class ExtensionManifest(BaseModel):
    """Base contract for all extension manifests.

    Subclassed by ConnectorManifest, BrickManifest, PluginManifest. The
    discriminator is the ``kind`` field — Pydantic v2 picks the subclass based
    on its value when parsing AnyManifest.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Kind
    module: str  # dotted path; NOT imported by manifest layer
    factory: str  # callable/class name in module
    description: str = ""
    runtime_deps: tuple[RuntimeDep, ...] = ()
    config_schema: str | None = None
    profile_gate: str | None = None
    import_probes: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v)

    @field_validator("module", "factory")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty")
        return v
