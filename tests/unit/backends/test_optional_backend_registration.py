"""Regression tests for ``nexus.backends._register_optional_backends()``.

Covers the round-7 review finding: hard registration failures
(SyntaxError in a connector module, ValueError from duplicate binding,
etc.) must NOT be swallowed — they must propagate so
``_optional_backends_registered`` stays False and the caller can retry
after fixing the bug instead of operating on a half-populated registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _reset_registration_flag() -> None:
    import nexus.backends as backends_mod

    backends_mod._optional_backends_registered = False


class TestRegisterOptionalBackendsFailureSemantics:
    def test_hard_error_propagates_and_leaves_flag_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ImportError during module import must not be swallowed.

        Before the fix, a ``SyntaxError`` / ``ValueError`` / any non-dep
        exception got logged and the registration flag was set anyway,
        leaving the process in a half-populated state with no retry.
        """
        import nexus.backends as backends_mod
        import nexus.backends._manifest as manifest_mod

        _reset_registration_flag()

        stub_entry = SimpleNamespace(
            name="_rt_regression_entry",
            module_path="nowhere.fake.module",
            class_name="Nope",
            description="regression",
            category="storage",
            runtime_deps=(),
            service_name=None,
        )

        real_import = backends_mod.importlib.import_module

        def _guarded_import(name: str, *args, **kwargs):
            if name == "nowhere.fake.module":
                raise RuntimeError("simulated connector module bug")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(manifest_mod, "CONNECTOR_MANIFEST", (stub_entry,))
        monkeypatch.setattr(
            backends_mod.importlib,
            "import_module",
            _guarded_import,
        )

        try:
            with pytest.raises(RuntimeError, match="simulated connector module bug"):
                backends_mod._register_optional_backends()

            assert backends_mod._optional_backends_registered is False, (
                "hard registration error must leave the flag unset so the next call can retry"
            )
        finally:
            from nexus.backends.base.registry import ConnectorRegistry

            try:
                ConnectorRegistry._base.unregister("_rt_regression_entry")
            except KeyError:
                pass
            _reset_registration_flag()
