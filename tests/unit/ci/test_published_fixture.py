from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/prepare_published_fixture.sh"


@pytest.mark.skipif(sys.platform == "win32", reason="published-image fixture runs under Unix bash")
def test_readiness_consumes_verbose_logs_without_a_broken_pipe(tmp_path):
    bash = shutil.which("bash")
    assert bash is not None
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
        "command = sys.argv[1]\n"
        "if command == 'logs':\n"
        "    sys.stdout.write('VFS data plane ready\\n' + 'startup log\\n' * 100000)\n"
        "elif command == 'inspect':\n"
        "    print('false')\n"
        "elif command == 'exec' and '--subject-type' in sys.argv:\n"
        "    print('/fixture-agent')\n"
        "elif command == 'cp' and sys.argv[2] == '-':\n"
        "    sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [bash, str(SCRIPT), "node", "client", "/tmp/fixture"],
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.stdout[-1000:], result.stderr[-1000:])
