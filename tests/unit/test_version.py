from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import nexus


def test_resolve_package_version_prefers_installed_metadata(monkeypatch) -> None:
    monkeypatch.setattr("nexus._package_version", lambda name: "1.2.3")
    assert nexus._resolve_package_version() == "1.2.3"


def test_resolve_package_version_falls_back_to_pyproject(monkeypatch, tmp_path) -> None:
    package_dir = tmp_path / "src" / "nexus"
    package_dir.mkdir(parents=True)
    fake_module = package_dir / "__init__.py"
    fake_module.write_text("# test module\n")
    (tmp_path / "pyproject.toml").write_text('version = "9.8.7"\n')

    monkeypatch.setattr(
        "nexus._package_version",
        lambda name: (_ for _ in ()).throw(PackageNotFoundError()),
    )
    monkeypatch.setattr(nexus, "__file__", str(fake_module))

    assert nexus._resolve_package_version() == "9.8.7"
