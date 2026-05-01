"""Pydantic discriminated-union manifest contract.

This module defines the data shape every extension (plugin, connector, brick)
declares in its sibling _manifest.py file. It must NOT import any
extension impl module — that boundary keeps introspection lazy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
