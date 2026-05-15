"""Generate API reference pages for nexus.fs modules.

Scoped to src/nexus/fs/ only — the slim package's public API surface.
Runs at build time via mkdocs-gen-files plugin.
"""

from pathlib import Path

import mkdocs_gen_files

nav = mkdocs_gen_files.Nav()
src_root = Path("../../src")
package_root = src_root / "nexus" / "fs"

for path in sorted(package_root.rglob("*.py")):
    module_path = path.relative_to(src_root).with_suffix("")
    doc_path = path.relative_to(src_root).with_suffix(".md")
    full_doc_path = Path("reference", doc_path)

    parts = tuple(module_path.parts)

    # Skip any module or package whose name starts with _
    # (except __init__.py which represents the package itself)
    if any(part.startswith("_") and part != "__init__" for part in parts):
        continue

    # __init__ represents the package — use index.md for nav
    if parts[-1] == "__init__":
        parts = parts[:-1]
        doc_path = doc_path.with_name("index.md")
        full_doc_path = full_doc_path.with_name("index.md")

    nav[parts] = doc_path.as_posix()

    with mkdocs_gen_files.open(full_doc_path, "w") as fd:
        identifier = ".".join(parts)
        print(f"::: {identifier}", file=fd)

    mkdocs_gen_files.set_edit_path(full_doc_path, path.relative_to(src_root))

# Write the literate-nav summary for the reference section
with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())
