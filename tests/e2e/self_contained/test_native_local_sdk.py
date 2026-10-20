"""Local SDK mounts must own their process and store bytes at the requested path."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.testkit.kernel import resolve_kernel_binary


@pytest.mark.e2e
@pytest.mark.parametrize("scenario", ["mounts", "workspace"])
def test_native_local_sdk(tmp_path: Path, scenario: str) -> None:
    binary = resolve_kernel_binary()
    if binary is None:
        if os.environ.get("CI"):
            pytest.fail("Native SDK acceptance requires the pinned cluster binary")
        pytest.skip("Native SDK acceptance requires the pinned cluster binary")
    home = tmp_path / "home"
    home.mkdir()
    system_variables = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "PROCESSOR_ARCHITECTURE",
        "COMSPEC",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in system_variables}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"),
        PYTHONUTF8="1",
        NEXUS_KERNEL_BINARY=str(binary.resolve()),
        NEXUS_FS_STATE_DIR=str(tmp_path / "state"),
        NEXUS_FS_PERSISTENT_DIR=str(tmp_path / "secrets"),
    )
    result = subprocess.run(
        [sys.executable, "-c", _SCENARIOS[scenario], str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_SCENARIOS = {
    "mounts": """
from pathlib import Path
import sys
from types import SimpleNamespace
import nexus.fs
from nexus.fs._kernel_factory import _KERNEL_CACHE

root = Path(sys.argv[1])
data = root / '真实目录'
other = root / 'other'
expected = bytes(range(256)) + b'Native durable facts'
name = '二进制.bin'
uri = 'local://' + data.as_posix()
first = nexus.fs.mount_sync(uri, at='/workspace', ephemeral=True)
process = first._kernel._kernel._shared.client._process
assert process is not None
second = None
try:
    first.write('/workspace/' + name, expected)
    assert (data / name).read_bytes() == expected
    assert not list((root / 'state').rglob(name))
    try:
        first._kernel.sys_setattr('/unsupported', entry_type=2,
                                  backend=SimpleNamespace(name='unsupported'))
    except ValueError as error:
        assert 'native construction parameters are required' in str(error)
    else:
        raise AssertionError('unsupported backends must not install phantom mounts')
    assert not first.exists('/unsupported')
    second = nexus.fs.mount_sync('local://' + other.as_posix(), at='/other', ephemeral=True)
    assert second._kernel._kernel._shared.client._process is process
    second.write('/other/keep.bin', b'second handle')
    first.close()
    first.close()
    assert process.poll() is None
    assert second.read('/other/keep.bin') == b'second handle'
finally:
    first.close()
    if second is not None:
        second.close()
assert process.poll() is not None
assert not _KERNEL_CACHE

reopened = nexus.fs.mount_sync(uri, at='/workspace', ephemeral=True)
try:
    assert reopened._kernel._kernel._shared.client._process is not process
    assert reopened.read('/workspace/' + name) == expected
finally:
    reopened.close()
assert not _KERNEL_CACHE
assert not list((root / 'state').glob('mounts.json'))

invalid = root / 'not-a-directory'
invalid.write_bytes(b'occupied')
try:
    nexus.fs.mount_sync('local://' + invalid.as_posix(), ephemeral=True)
except OSError:
    pass
else:
    raise AssertionError('mounting a file as a directory must fail')
assert not _KERNEL_CACHE
""",
    "workspace": """
from pathlib import Path
import sys
import nexus

root = Path(sys.argv[1]) / 'workspace'
expected = b'existing native root'
for turn, backend in enumerate(('path_local', 'local', 'path_local')):
    fs = nexus.connect(config={
        'profile': 'embedded', 'data_dir': str(root), 'backend': backend,
    })
    process = fs._kernel._process
    try:
        if turn == 0:
            fs.write('/existing.bin', expected)
            locations = list(root.rglob('existing.bin'))
            assert len(locations) == 1
            physical = locations[0]
        assert fs.read('/existing.bin') == expected
        assert physical.read_bytes() == expected
        assert not fs._mounted_backend_instances
    finally:
        fs.close()
    assert process.poll() is not None
""",
}
