"""Regression tests for slim-fs connector discovery (Issue #3830).

Covers the URI-path round-6 findings:

1. ``_discover_connector_module`` must NOT raise a raw
   ``ModuleNotFoundError`` for a scheme whose connector package tree
   does not exist — the caller expects to fall through to a friendly
   ``NexusURIError``.

2. Manifest alias schemes (e.g. ``gcalendar_connector`` whose
   ``module_path`` points at ``...connectors.calendar.connector``) must
   resolve through the registry without touching
   ``_discover_connector_module`` — otherwise the alias hits the same
   missing-package crash as an unknown scheme.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _MountSpec(SimpleNamespace):
    """Minimal MountSpec stand-in for _create_connector_backend()."""

    scheme: str
    authority: str
    path: str
    uri: str


def _spec(scheme: str, authority: str = "primary") -> _MountSpec:
    return _MountSpec(
        scheme=scheme,
        authority=authority,
        path="",
        uri=f"{scheme}://{authority}",
    )


class TestBackendFactoryDiscovery:
    def test_unknown_scheme_surfaces_nexus_uri_error(self) -> None:
        """``foobar://x`` must raise NexusURIError, not ModuleNotFoundError.

        Before the round-6 fix, ``_discover_connector_module`` re-raised
        the ModuleNotFoundError with ``name='nexus.backends.connectors.foobar'``
        because only ``exc.name == mod_path`` was treated as absent.
        """
        from nexus.contracts.exceptions import NexusURIError
        from nexus.fs._backend_factory import _create_connector_backend

        with pytest.raises(NexusURIError):
            _create_connector_backend(_spec("foobar"))

    def test_gcalendar_alias_routes_through_registry(self) -> None:
        """``gcalendar://`` is a manifest alias → must not hit discovery.

        The manifest registers ``gcalendar_connector`` at
        ``nexus.backends.connectors.calendar.connector``. The URI factory
        must treat ``gcalendar`` as a manifest-known scheme and look up
        via the registry; otherwise the discovery probe crashes on the
        non-existent ``nexus.backends.connectors.gcalendar`` package.

        We accept either a backend instance (full install) or the
        canonical ``MissingDependencyError`` (slim). We just require that
        no raw ``ModuleNotFoundError`` / ``NexusURIError`` escape.
        """
        from nexus.contracts.exceptions import (
            MissingDependencyError,
            NexusURIError,
        )
        from nexus.fs._backend_factory import _create_connector_backend

        try:
            _create_connector_backend(_spec("gcalendar"))
        except MissingDependencyError:
            # Slim install without googleapiclient — expected clean error.
            pass
        except ModuleNotFoundError as exc:  # pragma: no cover - regression
            pytest.fail(f"gcalendar alias leaked ModuleNotFoundError: {exc}")
        except NexusURIError as exc:  # pragma: no cover - regression
            pytest.fail(f"gcalendar alias not routed to registry: {exc}")
        except Exception:
            # Other errors (e.g. TokenManager missing during instantiation)
            # are unrelated to the regression under test.
            pass
