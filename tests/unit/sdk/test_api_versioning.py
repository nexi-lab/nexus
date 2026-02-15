"""Tests for API versioning decorators."""

import json
import tempfile
import types
from pathlib import Path

import pytest

from nexus.sdk.api_versioning import (
    collect_api_surface,
    diff_api_surfaces,
    experimental,
    export_api_surface,
    get_stable_since,
    is_experimental,
    is_stable_api,
    load_api_surface,
    stable_api,
)


class TestStableApiDecorator:
    """Test @stable_api decorator."""

    def test_marks_function(self) -> None:
        @stable_api(since="1.0")
        def my_func():
            pass

        assert is_stable_api(my_func)
        assert get_stable_since(my_func) == "1.0"

    def test_marks_class(self) -> None:
        @stable_api(since="2.0")
        class MyClass:
            pass

        assert is_stable_api(MyClass)
        assert get_stable_since(MyClass) == "2.0"

    def test_preserves_function_identity(self) -> None:
        """Decorator should return the original function, not a wrapper."""

        def original():
            return 42

        decorated = stable_api(since="1.0")(original)
        assert decorated is original
        assert decorated() == 42

    def test_preserves_class_identity(self) -> None:
        class Original:
            pass

        decorated = stable_api(since="1.0")(Original)
        assert decorated is Original

    def test_rejects_empty_since(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            stable_api(since="")

    def test_rejects_non_string_since(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            stable_api(since=123)  # type: ignore[arg-type]


class TestExperimentalDecorator:
    """Test @experimental decorator."""

    def test_marks_function(self) -> None:
        @experimental
        def my_func():
            pass

        assert is_experimental(my_func)
        assert not is_stable_api(my_func)

    def test_marks_class(self) -> None:
        @experimental
        class MyClass:
            pass

        assert is_experimental(MyClass)

    def test_preserves_function_identity(self) -> None:
        def original():
            return 42

        decorated = experimental(original)
        assert decorated is original
        assert decorated() == 42


class TestIntrospection:
    """Test introspection helpers."""

    def test_unmarked_function(self) -> None:
        def plain():
            pass

        assert not is_stable_api(plain)
        assert not is_experimental(plain)
        assert get_stable_since(plain) is None

    def test_stable_since(self) -> None:
        @stable_api(since="3.2.1")
        def func():
            pass

        assert get_stable_since(func) == "3.2.1"


class TestCollectApiSurface:
    """Test API surface collection."""

    def test_collects_stable_function(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @stable_api(since="1.0")
        def func():
            pass

        func.__module__ = "test_mod"
        mod.func = func

        surface = collect_api_surface(mod)
        assert len(surface) == 1
        assert surface[0]["name"] == "test_mod.func"
        assert surface[0]["stability"] == "stable"
        assert surface[0]["since"] == "1.0"

    def test_collects_experimental_class(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @experimental
        class MyClass:
            pass

        MyClass.__module__ = "test_mod"
        mod.MyClass = MyClass

        surface = collect_api_surface(mod)
        assert any(e["name"] == "test_mod.MyClass" for e in surface)
        entry = next(e for e in surface if e["name"] == "test_mod.MyClass")
        assert entry["stability"] == "experimental"
        assert entry["kind"] == "class"

    def test_skips_private_members(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @stable_api(since="1.0")
        def _private():
            pass

        _private.__module__ = "test_mod"
        mod._private = _private

        surface = collect_api_surface(mod)
        assert len(surface) == 0

    def test_skips_unmarked_items(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        def plain():
            pass

        plain.__module__ = "test_mod"
        mod.plain = plain

        surface = collect_api_surface(mod)
        assert len(surface) == 0

    def test_collects_class_methods(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @stable_api(since="1.0")
        class MyClass:
            @stable_api(since="1.0")
            def my_method(self):
                pass

        MyClass.__module__ = "test_mod"
        mod.MyClass = MyClass

        surface = collect_api_surface(mod)
        method_entries = [e for e in surface if e["kind"] == "method"]
        assert len(method_entries) == 1
        assert method_entries[0]["name"] == "test_mod.MyClass.my_method"


class TestExportImportSurface:
    """Test export/import of API surfaces."""

    def test_export_and_load(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @stable_api(since="1.0")
        def func():
            pass

        func.__module__ = "test_mod"
        mod.func = func

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "surface.json"
            count = export_api_surface(mod, path)

            assert count == 1
            assert path.exists()

            loaded = load_api_surface(path)
            assert len(loaded) == 1
            assert loaded[0]["name"] == "test_mod.func"

    def test_export_creates_parent_dirs(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "surface.json"
            export_api_surface(mod, path)
            assert path.exists()

    def test_export_json_format(self) -> None:
        mod = types.ModuleType("test_mod")
        mod.__name__ = "test_mod"

        @experimental
        def func():
            pass

        func.__module__ = "test_mod"
        mod.func = func

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "surface.json"
            export_api_surface(mod, path)

            with open(path) as f:
                data = json.load(f)

            assert data["module"] == "test_mod"
            assert data["count"] == 1
            assert isinstance(data["entries"], list)


class TestDiffApiSurfaces:
    """Test API surface diffing."""

    def test_no_changes(self) -> None:
        surface = [{"name": "mod.func", "stability": "stable", "since": "1.0"}]
        diff = diff_api_surfaces(surface, surface)
        assert diff["removed_stable"] == []
        assert diff["removed_experimental"] == []
        assert diff["added"] == []

    def test_detect_breaking_change(self) -> None:
        old = [
            {"name": "mod.func", "stability": "stable", "since": "1.0"},
        ]
        new: list[dict] = []

        diff = diff_api_surfaces(old, new)
        assert len(diff["removed_stable"]) == 1
        assert diff["removed_stable"][0]["name"] == "mod.func"

    def test_removing_experimental_is_not_breaking(self) -> None:
        old = [
            {"name": "mod.exp_func", "stability": "experimental"},
        ]
        new: list[dict] = []

        diff = diff_api_surfaces(old, new)
        assert len(diff["removed_stable"]) == 0
        assert len(diff["removed_experimental"]) == 1

    def test_detect_added_apis(self) -> None:
        old: list[dict] = []
        new = [
            {"name": "mod.new_func", "stability": "stable", "since": "2.0"},
        ]

        diff = diff_api_surfaces(old, new)
        assert len(diff["added"]) == 1
        assert diff["added"][0]["name"] == "mod.new_func"

    def test_mixed_changes(self) -> None:
        old = [
            {"name": "mod.stable_kept", "stability": "stable", "since": "1.0"},
            {"name": "mod.stable_removed", "stability": "stable", "since": "1.0"},
            {"name": "mod.exp_removed", "stability": "experimental"},
        ]
        new = [
            {"name": "mod.stable_kept", "stability": "stable", "since": "1.0"},
            {"name": "mod.new_func", "stability": "experimental"},
        ]

        diff = diff_api_surfaces(old, new)
        assert len(diff["removed_stable"]) == 1
        assert len(diff["removed_experimental"]) == 1
        assert len(diff["added"]) == 1
