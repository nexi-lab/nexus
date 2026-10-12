"""Exercise the build revision CLI used by image builds and CI."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "nexus_vfs_revision.py"
REPOSITORY = "https://github.com/nexi-lab/nexus-vfs"
REVISION = "1" * 40
OTHER_REVISION = "2" * 40


class BuildRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="nexus-vfs-revision-")
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "Cargo.toml"
        self.lock = self.manifest.with_name("Cargo.lock")
        self.manifest.write_text(
            f'[workspace.dependencies]\nkernel = {{ git = "{REPOSITORY}", rev = "{REVISION}" }}\n',
            encoding="utf8",
        )
        self.write_lock(f"git+{REPOSITORY}?rev={REVISION}#{REVISION}")

    def write_lock(self, source: str) -> None:
        self.lock.write_text(
            f'version = 4\n[[package]]\nname = "kernel"\nversion = "0.10.1"\nsource = "{source}"\n',
            encoding="utf8",
        )

    def run_cli(self, manifest: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(manifest or self.manifest)],
            capture_output=True,
            text=True,
            encoding="utf8",
            check=False,
        )

    def assert_rejected(self) -> None:
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid nexus-vfs build revision:", result.stderr)

    def test_repository_build_inputs(self) -> None:
        result = self.run_cli(ROOT / "Cargo.toml")
        with (ROOT / "Cargo.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), manifest["workspace"]["dependencies"]["kernel"]["rev"]
        )

    def test_toml_formatting_and_unrelated_dependencies(self) -> None:
        self.manifest.write_text(
            "[workspace.dependencies.kernel]\n"
            f'git = "{REPOSITORY}"\nrev = "{REVISION}"\n'
            "[workspace.dependencies.other]\n"
            f'git = "https://example.org/other"\nrev = "{OTHER_REVISION}"\n',
            encoding="utf8",
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, REVISION + "\n")

    def test_divergent_workspace_dependencies(self) -> None:
        with self.manifest.open("a", encoding="utf8") as stream:
            stream.write(f'contracts = {{ git = "{REPOSITORY}", rev = "{OTHER_REVISION}" }}\n')
        self.assert_rejected()

    def test_missing_or_invalid_manifest_revision(self) -> None:
        for field in ["", 'branch = "main"', 'rev = "short"', f'rev = "{"A" * 40}"', "rev = 42"]:
            with self.subTest(field=field):
                self.manifest.write_text(
                    f'[workspace.dependencies]\nkernel = {{ git = "{REPOSITORY}", {field} }}\n',
                    encoding="utf8",
                )
                self.assert_rejected()

    def test_missing_dependency_or_lockfile(self) -> None:
        self.manifest.write_text('[workspace.dependencies]\nserde = "1"\n', encoding="utf8")
        self.assert_rejected()
        self.manifest.write_text(
            f'[workspace.dependencies]\nkernel = {{ git = "{REPOSITORY}", rev = "{REVISION}" }}\n',
            encoding="utf8",
        )
        self.lock.unlink()
        self.assert_rejected()

    def test_lockfile_revision_and_resolved_commit_must_both_match(self) -> None:
        for source in [
            f"git+{REPOSITORY}?rev={OTHER_REVISION}#{OTHER_REVISION}",
            f"git+{REPOSITORY}?rev={REVISION}#{OTHER_REVISION}",
            f"git+{REPOSITORY}?rev={REVISION}",
        ]:
            with self.subTest(source=source):
                self.write_lock(source)
                self.assert_rejected()

    def test_every_resolved_package_must_match(self) -> None:
        with self.lock.open("a", encoding="utf8") as stream:
            stream.write(
                '[[package]]\nname = "contracts"\nversion = "0.1.0"\n'
                f'source = "git+{REPOSITORY}?rev={REVISION}#{OTHER_REVISION}"\n'
            )
        self.assert_rejected()

    def test_lockfile_must_include_nexus_vfs(self) -> None:
        self.write_lock("registry+https://github.com/rust-lang/crates.io-index")
        self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
