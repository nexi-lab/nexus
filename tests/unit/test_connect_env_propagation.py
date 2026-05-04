"""Unit tests for nexus.connect() env-var propagation (Codex review R3).

These tests guard the bridge that ``nexus.connect()`` builds between the
config layer (``cfg.profile`` / ``cfg.enable_vector_search``) and the
factory wiring layer (``_wired.py`` reads env vars). Without this
bridge, ``connect(config={"profile": "sandbox"})`` would never reach the
SANDBOX hybrid path because ``_wired.py`` derives the profile solely
from ``NEXUS_PROFILE``.

The full integration tests in ``tests/integration/test_sandbox_boot.py``
are blocked by an unrelated ``await nexus.connect()`` shape mismatch on
``develop``; these unit tests run synchronously by mocking out
``create_nexus_fs`` so we exercise only the propagation block.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest


class _BootEnvCapture:
    """Captures os.environ snapshot at the moment ``create_nexus_fs``
    is invoked. Codex review R4 (high) requires the env-propagation
    block to set vars BEFORE factory boot AND restore them AFTER, so
    asserting on ``os.environ`` after ``connect()`` returns is no
    longer valid — we must inspect the env from inside the boot
    scope."""

    def __init__(self) -> None:
        self.env_at_boot: dict[str, str | None] = {}

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        # Snapshot only the keys the propagation block can set.
        for k in (
            "NEXUS_PROFILE",
            "NEXUS_ENABLE_VECTOR_SEARCH",
        ):
            self.env_at_boot[k] = os.environ.get(k)
        nx = MagicMock(name="NexusFS")
        nx._memory_config = None
        nx._config = None
        return nx


def _patch_heavy_boot(monkeypatch: pytest.MonkeyPatch) -> _BootEnvCapture:
    """Patch the slow / external-touching bits of ``connect()`` so the
    test only exercises config + env propagation. Returns a capture
    object whose ``env_at_boot`` dict reflects ``os.environ`` at the
    moment ``create_nexus_fs`` was called — i.e. from INSIDE the
    propagation block, before the finally restores env."""
    capture = _BootEnvCapture()
    monkeypatch.setattr(
        "nexus.factory.create_nexus_fs",
        capture,
        raising=True,
    )
    monkeypatch.setattr(
        "nexus._open_local_metastore",
        lambda *a, **kw: MagicMock(name="MetastoreStub"),
        raising=False,
    )
    monkeypatch.setattr("nexus._restore_mounts", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("nexus._init_audit_hook", lambda *a, **kw: None, raising=False)
    return capture


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Strip every NEXUS_* var that could pollute env propagation. The
    propagation logic only writes a key when it is *absent* from env, so
    a leaked var from another test would mask real bugs."""
    for var in (
        "NEXUS_PROFILE",
        "NEXUS_ENABLE_VECTOR_SEARCH",
        "NEXUS_DISABLE_VECTOR_SEARCH",
        "NEXUS_HOSTNAME",
        "NEXUS_PEERS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


class TestProfilePropagation:
    """``cfg.profile`` must reach ``NEXUS_PROFILE`` before factory boot."""

    def test_sandbox_config_dict_sets_profile_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """``connect(config={"profile":"sandbox"})`` with no env must
        set ``NEXUS_PROFILE=sandbox`` so ``_wired.py``'s SANDBOX branch
        actually fires. Direct regression guard for Codex review R3."""
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
            }
        )
        assert capture.env_at_boot["NEXUS_PROFILE"] == "sandbox", (
            "config.profile must propagate to NEXUS_PROFILE before "
            "factory boot — without this, _wired.py's SANDBOX branch "
            "is dead code on the config-driven path."
        )

    def test_pre_existing_env_profile_wins_over_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Operator env precedence: if ``NEXUS_PROFILE`` is already in
        the environment, the propagation logic must NOT clobber it
        (matches the rest of the codebase's env-precedence convention)."""
        monkeypatch.setenv("NEXUS_PROFILE", "full")
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
            }
        )
        assert capture.env_at_boot["NEXUS_PROFILE"] == "full", (
            "operator-set NEXUS_PROFILE must take precedence over the "
            "config dict — matches the rest of the codebase's env-first "
            "precedence model"
        )


class TestVectorSearchPropagation:
    """An explicit ``enable_vector_search`` in the config dict must
    reach the env so ``_wired.py``'s opt-out path honors it. Without
    propagation, an explicit config opt-out is silently dropped on
    SANDBOX (vec defaults back to ON)."""

    def test_explicit_false_in_config_dict_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
                "enable_vector_search": False,
            }
        )
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "false", (
            "explicit enable_vector_search=False in the config dict must "
            "reach NEXUS_ENABLE_VECTOR_SEARCH so _wired.py's SANDBOX "
            "branch turns vec OFF"
        )

    def test_explicit_true_in_config_dict_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "full",
                "data_dir": str(tmp_path / "nx"),
                "enable_vector_search": True,
            }
        )
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "true"

    def test_unset_in_config_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """When the caller did not set enable_vector_search in the
        config dict, the env must remain untouched so _wired.py applies
        its profile-default (vec ON for SANDBOX, OFF for others). This
        is what lets SANDBOX's default-on path stay default-on."""
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
            }
        )
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] is None, (
            "key absent from config dict must NOT spawn an env var — "
            "_wired.py's profile-default must remain in control"
        )

    def test_pre_existing_env_wins_over_config_dict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """If the operator set NEXUS_ENABLE_VECTOR_SEARCH in env, the
        config dict value must NOT clobber it. Mirrors the standard
        env-precedence convention."""
        monkeypatch.setenv("NEXUS_ENABLE_VECTOR_SEARCH", "true")
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        # Try to override via config dict — env must win.
        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
                "enable_vector_search": False,
            }
        )
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "true"


class TestSourceAgnosticPropagation:
    """Codex review R4 (high): the propagation block must work
    regardless of input shape — config files, NexusConfig objects, and
    dicts all carry user-set fields and all must be honored."""

    def test_nexusconfig_object_input_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A NexusConfig instance (not a dict) with explicit
        enable_vector_search=False must still reach env."""
        from nexus.config import NexusConfig

        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        cfg = NexusConfig(
            profile="sandbox",
            data_dir=str(tmp_path / "nx"),
            enable_vector_search=False,
        )
        nexus.connect(config=cfg)
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "false", (
            "NexusConfig-typed input with explicit enable_vector_search=False "
            "must propagate (Codex R4 finding #1: dict-only check missed "
            "config files and NexusConfig instances)"
        )

    def test_yaml_file_input_propagates(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """A YAML config file with profile=sandbox + enable_vector_
        search=false must also propagate."""
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        cfg_path = tmp_path / "nexus.yaml"
        cfg_path.write_text(
            f"profile: sandbox\ndata_dir: {tmp_path / 'data'}\nenable_vector_search: false\n"
        )
        nexus.connect(config=str(cfg_path))
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "false", (
            "YAML file input with explicit enable_vector_search=false "
            "must propagate via cfg.model_fields_set"
        )


class TestEnvRestore:
    """Codex review R4 (high): env mutation must not leak across
    successive ``connect()`` calls or into subprocess env. The
    finally block restores prior state on both happy path and
    exception."""

    def test_env_restored_after_connect_returns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        # Pre-condition: env clean (autouse fixture wiped it).
        assert "NEXUS_PROFILE" not in os.environ
        assert "NEXUS_ENABLE_VECTOR_SEARCH" not in os.environ

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
                "enable_vector_search": False,
            }
        )

        # Boot saw the propagated values.
        assert capture.env_at_boot["NEXUS_PROFILE"] == "sandbox"
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "false"
        # But after return, env is back to its pre-connect state.
        assert "NEXUS_PROFILE" not in os.environ, (
            "connect() must NOT leave NEXUS_PROFILE in env after return — "
            "would leak into successive connect() calls and subprocesses"
        )
        assert "NEXUS_ENABLE_VECTOR_SEARCH" not in os.environ

    def test_pre_existing_env_preserved_after_connect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Operator-set env must survive connect() unchanged — the
        finally block restores the previous value, not None, when env
        was already set."""
        monkeypatch.setenv("NEXUS_PROFILE", "full")
        monkeypatch.setenv("NEXUS_ENABLE_VECTOR_SEARCH", "true")
        _patch_heavy_boot(monkeypatch)
        import nexus

        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx"),
                "enable_vector_search": False,
            }
        )

        assert os.environ.get("NEXUS_PROFILE") == "full", (
            "operator env must be unchanged after connect() returns"
        )
        assert os.environ.get("NEXUS_ENABLE_VECTOR_SEARCH") == "true"

    def test_env_restored_when_boot_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Exception during factory boot must still trigger env
        restoration — the finally block runs even on raised exceptions.
        Otherwise a single failed boot leaks synthetic env into all
        future opens of the same process."""

        def _boot_explodes(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated boot failure")

        monkeypatch.setattr("nexus.factory.create_nexus_fs", _boot_explodes, raising=True)
        monkeypatch.setattr(
            "nexus._open_local_metastore",
            lambda *a, **kw: MagicMock(name="MetastoreStub"),
            raising=False,
        )

        import nexus

        with pytest.raises(RuntimeError, match="simulated boot failure"):
            nexus.connect(
                config={
                    "profile": "sandbox",
                    "data_dir": str(tmp_path / "nx"),
                    "enable_vector_search": False,
                }
            )

        # Even though boot raised, env must be restored.
        assert "NEXUS_PROFILE" not in os.environ
        assert "NEXUS_ENABLE_VECTOR_SEARCH" not in os.environ

    def test_two_sequential_connects_do_not_leak(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The headline R4 #2 scenario: connect with sandbox/vec-off
        first, then connect with full/vec-on. The second call must NOT
        see leaked sandbox/false env from the first."""
        capture = _patch_heavy_boot(monkeypatch)
        import nexus

        # First call: sandbox + opt-out.
        nexus.connect(
            config={
                "profile": "sandbox",
                "data_dir": str(tmp_path / "nx1"),
                "enable_vector_search": False,
            }
        )
        # Second call: full + opt-in. Must see full/true at boot, not
        # the leaked sandbox/false from call #1.
        nexus.connect(
            config={
                "profile": "full",
                "data_dir": str(tmp_path / "nx2"),
                "enable_vector_search": True,
            }
        )
        assert capture.env_at_boot["NEXUS_PROFILE"] == "full", (
            "sequential connect() calls must not leak env between boots — "
            "headline regression for Codex R4 finding #2"
        )
        assert capture.env_at_boot["NEXUS_ENABLE_VECTOR_SEARCH"] == "true"


class TestConcurrentConnectIsSerialized:
    """Codex review R5 (high): the env-mutation + factory-boot region
    must run under a process-wide lock so two threads calling
    ``connect()`` concurrently don't see each other's synthetic env
    as 'operator-set' and skip propagation."""

    def test_concurrent_connects_each_see_their_own_propagated_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Run two connect() calls in parallel threads with different
        profile values. With the lock, each call's boot must observe
        ITS OWN profile, not a leaked value from the other thread."""
        import threading

        # Both threads' create_nexus_fs callbacks funnel through one
        # shared stub that snapshots os.environ['NEXUS_PROFILE'] at
        # call time. With the lock in place, the two snapshots must
        # be {sandbox, full} (each thread saw its own propagated env).
        # Without the lock, both threads could observe the same value
        # because thread B's "is env empty?" check ran while thread
        # A's synthetic env was still in place.
        captured_profile_for_each_call: list[str | None] = []

        def _shared_factory(*_a: Any, **_kw: Any) -> Any:
            captured_profile_for_each_call.append(os.environ.get("NEXUS_PROFILE"))
            nx = MagicMock(name="NexusFS")
            nx._memory_config = None
            nx._config = None
            return nx

        monkeypatch.setattr("nexus.factory.create_nexus_fs", _shared_factory, raising=True)
        monkeypatch.setattr(
            "nexus._open_local_metastore",
            lambda *a, **kw: MagicMock(name="MetastoreStub"),
            raising=False,
        )
        monkeypatch.setattr("nexus._restore_mounts", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("nexus._init_audit_hook", lambda *a, **kw: None, raising=False)

        import nexus

        def _call_with_profile(profile: str, dirname: str) -> None:
            nexus.connect(
                config={
                    "profile": profile,
                    "data_dir": str(tmp_path / dirname),
                }
            )

        ta = threading.Thread(target=_call_with_profile, args=("sandbox", "a"))
        tb = threading.Thread(target=_call_with_profile, args=("full", "b"))
        ta.start()
        tb.start()
        ta.join(timeout=10.0)
        tb.join(timeout=10.0)

        assert not ta.is_alive() and not tb.is_alive(), "thread hang — lock deadlock?"

        # Each call observed exactly one profile snapshot. Because the
        # lock serializes, the snapshot list has one of each profile.
        assert sorted(p for p in captured_profile_for_each_call if p is not None) == [
            "full",
            "sandbox",
        ], (
            f"both threads must each see their own propagated profile, got "
            f"{captured_profile_for_each_call}. If both show the same value, "
            f"the lock is missing or scoped too narrowly."
        )
