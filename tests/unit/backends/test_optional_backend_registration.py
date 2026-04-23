"""Regression tests for ``nexus.backends._register_optional_backends()``.

Covers round-9 review finding: a single connector's hard import
failure must NOT abort global registration (that would block mounting
every other unrelated connector in the same process). Instead, the
failure is captured on that connector's placeholder via
``record_import_failure`` and the loop continues with the next module.
``BackendFactory.create()`` later surfaces the captured error only for
the specific connector that is actually mounted.

Oscillation history:
- Round 5 swallowed all exceptions silently (hid bugs).
- Round 7 re-raised non-ImportError (fail-loud, but blast radius).
- Round 9 (this file) captures per-entry and continues.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _reset_registration_flag() -> None:
    import nexus.backends as backends_mod

    backends_mod._optional_backends_registered = False


class TestRegisterOptionalBackendsFailureSemantics:
    def test_hard_error_is_contained_per_connector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single module's hard import failure must not abort registration.

        One broken connector module must not take out mounts for every
        other unrelated connector sharing this process. The failure is
        captured on the broken entry's placeholder and the loop
        continues registering the remainder.
        """
        import nexus.backends as backends_mod
        import nexus.backends._manifest as manifest_mod

        _reset_registration_flag()

        broken_entry = SimpleNamespace(
            name="_rt_regression_broken",
            module_path="nowhere.fake.module",
            class_name="Broken",
            description="broken",
            category="storage",
            runtime_deps=(),
            service_name=None,
        )
        healthy_entry = SimpleNamespace(
            name="_rt_regression_healthy",
            module_path="json",  # pre-installed stdlib, always imports
            class_name="JSONDecoder",
            description="healthy",
            category="storage",
            runtime_deps=(),
            service_name=None,
        )

        real_import = backends_mod.importlib.import_module

        def _guarded_import(name: str, *args, **kwargs):
            if name == "nowhere.fake.module":
                raise RuntimeError("simulated connector module bug")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(
            manifest_mod,
            "CONNECTOR_MANIFEST",
            (broken_entry, healthy_entry),
        )
        monkeypatch.setattr(
            backends_mod.importlib,
            "import_module",
            _guarded_import,
        )

        from nexus.backends.base.registry import ConnectorRegistry

        try:
            # Must NOT raise — registration survives a single bad module.
            backends_mod._register_optional_backends()

            assert backends_mod._optional_backends_registered is True, (
                "registration must complete even when one module fails"
            )

            broken = ConnectorRegistry.get_info("_rt_regression_broken")
            assert broken.connector_class is None
            assert broken.import_error is not None
            assert "simulated connector module bug" in broken.import_error
            assert "RuntimeError" in broken.import_error

            # Unrelated connector still registered normally — no spill-over.
            healthy = ConnectorRegistry.get_info("_rt_regression_healthy")
            assert healthy.import_error is None
        finally:
            for n in ("_rt_regression_broken", "_rt_regression_healthy"):
                try:
                    ConnectorRegistry._base.unregister(n)
                except KeyError:
                    pass
            _reset_registration_flag()
