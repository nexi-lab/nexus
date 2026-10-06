#!/usr/bin/env python3
"""Prepare local dependencies for the coordinated session mailbox changes.

Builds the real Node package and installs it with Bun --no-save. Generates an
external Cargo patch file so all Nexus crates resolve to the same checkout.
Published version pins and repository lockfiles are left for the release step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

NEXUS_CRATES = {
    "a2a": "rust/a2a",
    "backends": "rust/backends",
    "contracts": "rust/contracts",
    "kernel": "rust/kernel",
    "lib": "rust/lib",
    "managed-agent": "rust/managed_agent",
    "llm-mount": "rust/llm_mount",
    "nexus-cluster": "rust/profiles/cluster",
}


def run(argv: list[str], cwd: Path) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True, encoding="utf-8").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nexus-vfs", type=Path, required=True)
    parser.add_argument("--sudocode", type=Path, required=True)
    parser.add_argument("--moss", type=Path)
    parser.add_argument("--sudowork", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    vfs, sudocode, output = args.nexus_vfs.resolve(), args.sudocode.resolve(), args.output.resolve()
    manifest = tomllib.loads((sudocode / "rust/Cargo.toml").read_text(encoding="utf-8"))
    dependencies = {
        name
        for name, dep in manifest["workspace"]["dependencies"].items()
        if isinstance(dep, dict)
        and dep.get("git", "").rstrip("/") == "https://github.com/nexi-lab/nexus-vfs"
    }
    if dependencies != NEXUS_CRATES.keys():
        raise SystemExit(
            f"Update the complete Nexus crate mapping before continuing: {sorted(dependencies)}"
        )
    for name, relative in NEXUS_CRATES.items():
        package = tomllib.loads((vfs / relative / "Cargo.toml").read_text(encoding="utf-8"))
        if package["package"]["name"] != name:
            raise SystemExit(f"Wrong crate at {relative}")
    output.mkdir(parents=True, exist_ok=True)
    patch = output / "nexus-vfs-patches.toml"
    lines = ['[patch."https://github.com/nexi-lab/nexus-vfs"]']
    lines += [
        f"{name} = {{ path = {json.dumps((vfs / relative).as_posix())} }}"
        for name, relative in NEXUS_CRATES.items()
    ]
    patch.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    npm = shutil.which("npm")
    bun = shutil.which("bun")
    if not npm or ((args.moss or args.sudowork) and not bun):
        raise SystemExit("Install npm and Bun before preparing the workspace")
    sdk = vfs / "clients/node"
    subprocess.run([npm, "run", "build"], cwd=sdk, check=True)
    subprocess.run(
        [npm, "pack", "--ignore-scripts", "--pack-destination", str(output)], cwd=sdk, check=True
    )
    package = json.loads((sdk / "package.json").read_text(encoding="utf-8"))
    filename = package["name"].removeprefix("@").replace("/", "-")
    tarball = output / f"{filename}-{package['version']}.tgz"
    consumers = []
    if args.moss:
        consumers.append(args.moss.resolve())
    if args.sudowork:
        consumers.append(args.sudowork.resolve() / "apps/desktop")
    for consumer in consumers:
        relative = Path(os.path.relpath(tarball, consumer)).as_posix()
        dependency = f"{package['name']}@file:{relative}"
        subprocess.run(
            [bun, "add", "--no-save", "--ignore-scripts", dependency], cwd=consumer, check=True
        )
    if args.moss:
        # Bun removes unlisted vendor packages when changing the SDK. Restore
        # the repository's normal postinstall before building real server tests.
        subprocess.run([bun, "run", "scripts/copy-vendor.js"], cwd=args.moss.resolve(), check=True)
    report = {
        "protocol": "acp-mailbox/1",
        "sdk": str(tarball),
        "sdk_sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
        "cargo_config": str(patch),
        "nexus_vfs_commit": run(["git", "rev-parse", "HEAD"], vfs),
        "sudocode_commit": run(["git", "rev-parse", "HEAD"], sudocode),
        "nexus_vfs_dirty": bool(run(["git", "status", "--porcelain"], vfs)),
        "sudocode_dirty": bool(run(["git", "status", "--porcelain"], sudocode)),
    }
    (output / "workspace.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(report, indent=2))
    print(f"Run cargo from {sudocode / 'rust'} with --config {patch}")
    print("Cargo may regenerate its lock for the local patch graph; do not commit that local lock.")


if __name__ == "__main__":
    main()
