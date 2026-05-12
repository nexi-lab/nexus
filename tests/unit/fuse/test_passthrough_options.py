from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("nexus_runtime", MagicMock())

from nexus.fuse.mount import MountMode, NexusFUSE, mount_nexus  # noqa: E402
from nexus.fuse.passthrough import (  # noqa: E402
    PassthroughOptions,
    RustPassthroughMount,
    mount_is_passthrough_safe,
)


def test_options_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH", "true")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_PATTERNS", "/data/**, /models/*.bin")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS", "/data/private/**")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", "262144")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_REQUIRE", "1")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_BACKING_DIR", "/tmp/nexus-passthrough")

    options = PassthroughOptions.from_env()

    assert options.enabled is True
    assert options.patterns == ["/data/**", "/models/*.bin"]
    assert options.deny_patterns == ["/data/private/**"]
    assert options.threshold_bytes == 262144
    assert options.require is True
    assert options.backing_dir == Path("/tmp/nexus-passthrough")


def test_rust_mount_command_uses_api_key_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH", "true")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_PATTERNS", "/env/**")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS", "/env/private/**")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", "999")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_REQUIRE", "1")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_BACKING_DIR", "/tmp/env-backing")
    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(
            enabled=True,
            patterns=["/data/**"],
            deny_patterns=["/data/private/**"],
            threshold_bytes=131072,
        ),
        agent_id="agent-1",
    )

    cmd, env, api_key_file = mount.build_command()
    try:
        assert cmd[:4] == ["/usr/bin/nexus-fuse", "mount", str(tmp_path / "mnt"), "--url"]
        assert "sk-test" not in " ".join(cmd)
        assert "--api-key-file" in cmd
        assert "--passthrough" in cmd
        assert cmd.count("--passthrough-pattern") == 1
        assert cmd[cmd.index("--passthrough-pattern") + 1] == "/data/**"
        assert cmd.count("--passthrough-deny-pattern") == 1
        assert cmd[cmd.index("--passthrough-deny-pattern") + 1] == "/data/private/**"
        assert "--passthrough-threshold-bytes" in cmd
        assert cmd[cmd.index("--passthrough-threshold-bytes") + 1] == "131072"
        assert cmd[cmd.index("--agent-id") + 1] == "agent-1"
        assert "NEXUS_API_KEY" not in env
        assert not any(key.startswith("NEXUS_FUSE_PASSTHROUGH") for key in env)
        assert api_key_file.read_text() == "sk-test"
        assert oct(api_key_file.stat().st_mode & 0o777) == "0o600"
    finally:
        mount.stop()


def test_mount_safety_denies_context_and_hook_counts(monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = MagicMock()
    fs.mount_hook_count = 0
    fs.unmount_hook_count = 0

    fs._kernel.hook_count.side_effect = lambda name: 1 if name == "read" else 0
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False
    fs._kernel.hook_count.side_effect = lambda name: 1 if name == "stat" else 0
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False
    fs._kernel.hook_count.side_effect = lambda name: 1 if name == "open" else 0
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False
    fs._kernel.hook_count.side_effect = lambda name: 0
    assert mount_is_passthrough_safe(fs, mode_value="smart", context=None) is False
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=object()) is False
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is True


@pytest.mark.parametrize(
    "hook_name",
    [
        "write",
        "write_batch",
        "delete",
        "rename",
        "copy",
        "mkdir",
        "rmdir",
        "access",
    ],
)
def test_mount_safety_denies_mutating_and_access_hooks(monkeypatch, hook_name: str):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = MagicMock()
    fs.mount_hook_count = 0
    fs.unmount_hook_count = 0
    fs._kernel.hook_count.side_effect = lambda name: 1 if name == hook_name else 0

    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False


@pytest.mark.parametrize("lifecycle_attr", ["mount_hook_count", "unmount_hook_count"])
def test_mount_safety_denies_lifecycle_hooks(monkeypatch, lifecycle_attr: str):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = MagicMock()
    fs.mount_hook_count = 0
    fs.unmount_hook_count = 0
    setattr(fs, lifecycle_attr, 1)
    fs._kernel.hook_count.side_effect = lambda name: 0

    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False


def test_mount_safety_denies_hook_count_exception_or_missing(monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    raising_fs = MagicMock()
    raising_fs.mount_hook_count = 0
    raising_fs.unmount_hook_count = 0
    raising_fs._kernel.hook_count.side_effect = RuntimeError("hook count unavailable")

    missing_hook_count_fs = SimpleNamespace(
        _kernel=SimpleNamespace(),
        mount_hook_count=0,
        unmount_hook_count=0,
    )

    assert mount_is_passthrough_safe(raising_fs, mode_value="binary", context=None) is False
    assert (
        mount_is_passthrough_safe(missing_hook_count_fs, mode_value="binary", context=None) is False
    )


def test_mount_safety_denies_lifecycle_hook_count_exception(monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")

    class RaisesLifecycleHookCount:
        _kernel = SimpleNamespace(hook_count=lambda name: 0)

        @property
        def mount_hook_count(self):
            raise RuntimeError("mount hook count unavailable")

        @property
        def unmount_hook_count(self):
            return 0

    assert (
        mount_is_passthrough_safe(RaisesLifecycleHookCount(), mode_value="binary", context=None)
        is False
    )


def test_start_waits_for_mount_and_keeps_process_output_in_log(monkeypatch, tmp_path: Path):
    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
    )
    popen = MagicMock()
    popen.return_value.poll.return_value = None
    monkeypatch.setattr("nexus.fuse.passthrough.subprocess.Popen", popen)
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    mount.start()
    try:
        assert popen.call_args.kwargs["stdout"] is not subprocess.DEVNULL
        assert popen.call_args.kwargs["stderr"] is subprocess.STDOUT
        assert mount.log_file is not None
    finally:
        mount.stop()


def test_start_raises_and_cleans_up_when_process_exits_before_mount(monkeypatch, tmp_path: Path):
    api_key_file = None

    process = MagicMock()
    process.poll.return_value = 2

    def popen(cmd, **kwargs):
        nonlocal api_key_file
        api_key_file = Path(cmd[cmd.index("--api-key-file") + 1])
        assert api_key_file.exists()
        return process

    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
    )
    monkeypatch.setattr("nexus.fuse.passthrough.subprocess.Popen", popen)
    monkeypatch.setattr(Path, "is_mount", lambda self: False)

    with pytest.raises(RuntimeError, match="exited before mounting"):
        mount.start()

    assert api_key_file is not None
    assert not api_key_file.exists()
    assert mount.process is None
    assert mount.api_key_file is None


def test_start_cleans_api_key_file_when_popen_fails(monkeypatch, tmp_path: Path):
    api_key_file = None

    def raise_from_popen(cmd, **kwargs):
        nonlocal api_key_file
        api_key_file = Path(cmd[cmd.index("--api-key-file") + 1])
        assert api_key_file.exists()
        raise RuntimeError("failed to start")

    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
    )
    monkeypatch.setattr("nexus.fuse.passthrough.subprocess.Popen", raise_from_popen)

    with pytest.raises(RuntimeError, match="failed to start"):
        mount.start()

    assert api_key_file is not None
    assert not api_key_file.exists()
    assert mount.api_key_file is None


def test_start_rejects_repeated_start_without_touching_running_mount(tmp_path: Path):
    api_key_file = tmp_path / "api-key"
    api_key_file.write_text("sk-test")
    process = MagicMock()
    process.poll.return_value = None
    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
        process=process,
        api_key_file=api_key_file,
    )

    with pytest.raises(RuntimeError, match="already running"):
        mount.start()

    assert mount.process is process
    assert mount.api_key_file == api_key_file
    assert api_key_file.read_text() == "sk-test"


def test_stop_cleans_process_and_api_key_file_on_timeout(tmp_path: Path):
    api_key_file = tmp_path / "api-key"
    api_key_file.write_text("sk-test")
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="nexus-fuse", timeout=5)
    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
        process=process,
        api_key_file=api_key_file,
    )

    try:
        mount.stop()
    except subprocess.TimeoutExpired:
        pass

    assert mount.process is None
    assert mount.api_key_file is None
    assert not api_key_file.exists()


def test_create_finds_rust_binary_without_starting_daemon(monkeypatch, tmp_path: Path):
    def fail_start_daemon(self):
        raise AssertionError("create should not start the Rust daemon")

    monkeypatch.setattr(
        "nexus.fuse.passthrough.RustFUSEClient._find_rust_binary",
        lambda self: "/usr/bin/nexus-fuse",
    )
    monkeypatch.setattr("nexus.fuse.passthrough.RustFUSEClient._start_daemon", fail_start_daemon)

    mount = RustPassthroughMount.create(
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"]),
        agent_id="agent-1",
    )

    assert mount.rust_binary == "/usr/bin/nexus-fuse"
    assert mount.agent_id == "agent-1"


def test_nexus_fuse_uses_rust_passthrough_launcher_when_safe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = MagicMock()
    fs._base_url = "http://localhost:2026"
    fs._api_key = "sk-test"
    fs._kernel.hook_count.return_value = 0
    fs.mount_hook_count = 0
    fs.unmount_hook_count = 0
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    launcher = MagicMock()
    with (
        patch("nexus.fuse.mount.RustPassthroughMount.create", return_value=launcher) as create,
        patch("nexus.fuse.mount.NexusFUSEOperations") as operations_cls,
        patch("nexus.fuse.mount.FUSE") as fuse_cls,
        patch.object(NexusFUSE, "_start_warmup"),
    ):
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
            passthrough_patterns=["/data/**"],
        )
        fuse.mount(foreground=False)

    create.assert_called_once()
    launcher.start.assert_called_once_with()
    operations_cls.assert_not_called()
    fuse_cls.assert_not_called()
    assert fuse.is_mounted() is True


def test_nexus_fuse_uses_remote_profile_passthrough_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = SimpleNamespace(
        _remote_base_url="http://remote.example:2026",
        _remote_api_key="sk-remote",
        _kernel=SimpleNamespace(hook_count=lambda _name: 0),
        mount_hook_count=0,
        unmount_hook_count=0,
    )
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    launcher = MagicMock()
    with (
        patch("nexus.fuse.mount.RustPassthroughMount.create", return_value=launcher) as create,
        patch("nexus.fuse.mount.NexusFUSEOperations") as operations_cls,
        patch("nexus.fuse.mount.FUSE") as fuse_cls,
        patch.object(NexusFUSE, "_start_warmup"),
    ):
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
        )
        fuse.mount(foreground=False)

    assert create.call_args.kwargs["nexus_url"] == "http://remote.example:2026"
    assert create.call_args.kwargs["api_key"] == "sk-remote"
    launcher.start.assert_called_once_with()
    operations_cls.assert_not_called()
    fuse_cls.assert_not_called()


def test_nexus_fuse_allows_missing_remote_api_key_as_empty_string(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = SimpleNamespace(
        _remote_base_url="http://remote.example:2026",
        _kernel=SimpleNamespace(hook_count=lambda _name: 0),
        mount_hook_count=0,
        unmount_hook_count=0,
    )
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    launcher = MagicMock()
    with (
        patch("nexus.fuse.mount.RustPassthroughMount.create", return_value=launcher) as create,
        patch.object(NexusFUSE, "_start_warmup"),
    ):
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
        )
        fuse.mount(foreground=False)

    assert create.call_args.kwargs["api_key"] == ""


def test_remote_connect_persists_passthrough_credentials(monkeypatch):
    import nexus

    cfg = SimpleNamespace(
        profile="remote",
        url="http://configured.example:2026",
        api_key="sk-config",
        timeout=30,
        connect_timeout=5,
    )
    monkeypatch.setenv("NEXUS_GRPC_TLS", "false")
    monkeypatch.setattr("nexus.config.load_config", lambda _config: cfg)
    monkeypatch.setattr(nexus, "_open_local_kernel", lambda _path: MagicMock())

    class FakeTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.call_rpc = MagicMock()

    class FakeNexusFS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.setattr_calls = []
            self.closeables = []

        def sys_setattr(self, *args, **kwargs):
            self.setattr_calls.append((args, kwargs))

        def _register_runtime_closeable(self, closeable):
            self.closeables.append(closeable)

    monkeypatch.setitem(
        sys.modules,
        "nexus.remote.rpc_transport",
        types.SimpleNamespace(RPCTransport=FakeTransport),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexus.contracts.metadata",
        types.SimpleNamespace(DT_MOUNT="mount"),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexus.contracts.types",
        types.SimpleNamespace(OperationContext=lambda **kwargs: kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexus.core.config",
        types.SimpleNamespace(PermissionConfig=lambda **kwargs: kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexus.core.nexus_fs",
        types.SimpleNamespace(NexusFS=FakeNexusFS),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexus.factory._remote",
        types.SimpleNamespace(
            _boot_remote_services=lambda *args, **kwargs: None,
            install_remote_kernel_rpc_overrides=lambda *args, **kwargs: None,
        ),
    )

    nfs = nexus.connect(config={"profile": "remote"})

    assert nfs._remote_base_url == "http://configured.example:2026"
    assert nfs._remote_api_key == "sk-config"


def test_nexus_fuse_falls_back_when_passthrough_not_safe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Darwin")
    fs = MagicMock()
    fs._base_url = "http://localhost:2026"
    fs._api_key = "sk-test"
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    with patch("nexus.fuse.mount.FUSE") as fuse_cls:
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
        )
        fuse.mount(foreground=True)

    fuse_cls.assert_called_once()


def test_nexus_fuse_passthrough_require_raises_when_unsafe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Darwin")
    fs = MagicMock()
    fs._base_url = "http://localhost:2026"
    fs._api_key = "sk-test"
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    fuse = NexusFUSE(
        fs,
        str(mount_point),
        mode=MountMode.BINARY,
        use_rust=True,
        passthrough_enabled=True,
        passthrough_require=True,
    )

    with pytest.raises(RuntimeError, match="passthrough is not safe"):
        fuse.mount(foreground=False)


def test_nexus_fuse_passthrough_require_raises_for_local_fs(tmp_path: Path):
    fs = SimpleNamespace()
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    fuse = NexusFUSE(
        fs,
        str(mount_point),
        mode=MountMode.BINARY,
        use_rust=True,
        passthrough_enabled=True,
        passthrough_require=True,
    )

    with pytest.raises(RuntimeError, match="requires a remote NexusFS"):
        fuse.mount(foreground=False)


def test_mount_nexus_threads_passthrough_args_to_nexus_fuse(tmp_path: Path):
    fs = MagicMock()
    mount_point = tmp_path / "mnt"
    backing_dir = tmp_path / "backing"

    with patch("nexus.fuse.mount.NexusFUSE") as fuse_cls:
        mount_nexus(
            fs,
            str(mount_point),
            mode="binary",
            foreground=False,
            passthrough_enabled=True,
            passthrough_patterns=["/data/**"],
            passthrough_deny_patterns=["/data/private/**"],
            passthrough_threshold_bytes=4096,
            passthrough_require=True,
            passthrough_backing_dir=backing_dir,
        )

    fuse_cls.assert_called_once()
    assert fuse_cls.call_args.kwargs["passthrough_enabled"] is True
    assert fuse_cls.call_args.kwargs["passthrough_patterns"] == ["/data/**"]
    assert fuse_cls.call_args.kwargs["passthrough_deny_patterns"] == ["/data/private/**"]
    assert fuse_cls.call_args.kwargs["passthrough_threshold_bytes"] == 4096
    assert fuse_cls.call_args.kwargs["passthrough_require"] is True
    assert fuse_cls.call_args.kwargs["passthrough_backing_dir"] == backing_dir
    fuse_cls.return_value.mount.assert_called_once_with(
        foreground=False, allow_other=False, debug=False
    )


def test_nexus_fuse_uses_env_passthrough_threshold_when_not_overridden(monkeypatch):
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", "262144")

    fuse = NexusFUSE(MagicMock(), "/mnt", mode=MountMode.BINARY)

    assert fuse._passthrough_options.threshold_bytes == 262144


def test_unmount_stops_rust_passthrough_launcher_on_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    fs = MagicMock()
    fuse = NexusFUSE(fs, str(tmp_path), mode=MountMode.BINARY)
    launcher = MagicMock()
    fuse._rust_passthrough_mount = launcher
    fuse._mounted = True

    fuse.unmount()

    run.assert_called_once()
    launcher.stop.assert_called_once_with()
    assert fuse._rust_passthrough_mount is None


def test_unmount_retains_rust_passthrough_launcher_when_stop_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("subprocess.run", MagicMock())
    fs = MagicMock()
    fuse = NexusFUSE(fs, str(tmp_path), mode=MountMode.BINARY)
    launcher = MagicMock()
    launcher.stop.side_effect = RuntimeError("stop failed")
    fuse._rust_passthrough_mount = launcher
    fuse._mounted = True

    with pytest.raises(RuntimeError, match="stop failed"):
        fuse.unmount()

    launcher.stop.assert_called_once_with()
    assert fuse._rust_passthrough_mount is launcher

    launcher.stop.side_effect = None
    fuse.unmount()

    assert launcher.stop.call_count == 2
    assert fuse._rust_passthrough_mount is None


def test_unmount_stops_rust_passthrough_launcher_on_platform_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")

    def fail_unmount(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr=b"busy",
        )

    monkeypatch.setattr("subprocess.run", fail_unmount)
    fs = MagicMock()
    fuse = NexusFUSE(fs, str(tmp_path), mode=MountMode.BINARY)
    launcher = MagicMock()
    fuse._rust_passthrough_mount = launcher
    fuse._mounted = True

    with pytest.raises(RuntimeError, match="busy"):
        fuse.unmount()

    launcher.stop.assert_called_once_with()
    assert fuse._rust_passthrough_mount is None


def test_unmount_retains_rust_passthrough_launcher_when_platform_and_stop_fail(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("platform.system", lambda: "Linux")

    def fail_unmount(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr=b"busy",
        )

    monkeypatch.setattr("subprocess.run", fail_unmount)
    fs = MagicMock()
    fuse = NexusFUSE(fs, str(tmp_path), mode=MountMode.BINARY)
    launcher = MagicMock()
    launcher.stop.side_effect = RuntimeError("stop failed")
    fuse._rust_passthrough_mount = launcher
    fuse._mounted = True

    with pytest.raises(RuntimeError, match="busy"):
        fuse.unmount()

    launcher.stop.assert_called_once_with()
    assert fuse._rust_passthrough_mount is launcher
