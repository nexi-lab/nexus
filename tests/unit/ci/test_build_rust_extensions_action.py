from __future__ import annotations

from pathlib import Path


def test_macos_arm64_uses_native_protoc_with_legacy_version_wrapper() -> None:
    action = Path(".github/actions/build-rust-extensions/action.yml").read_text()

    assert 'macOS-ARM64) USE_GRPC_TOOLS_PROTOC="1"' in action
    assert 'python -m pip install "grpcio-tools>=1.80,<2"' in action
    assert "python -m grpc_tools.protoc" in action
    assert 'echo "libprotoc 3.20.3"' in action
    assert 'macOS-ARM64) ASSET="protoc-3.20.3-osx-x86_64.zip"' not in action
