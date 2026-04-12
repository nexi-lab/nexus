"""Readme documentation generator — extracted from ReadmeDocMixin.

Converts connector metadata (Pydantic schemas, operation traits, error
registries) into README.md markdown and a virtual ``.readme/`` tree used
by the read-path overlay (Issue #3728).
"""

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from nexus.backends.connectors.base import ConfirmLevel, ErrorDef, OpTraits

logger = logging.getLogger(__name__)


# =============================================================================
# Virtual .readme/ tree model (Issue #3728)
# =============================================================================


@dataclass
class VirtualEntry:
    """A node in the virtual ``.readme/`` filesystem tree.

    Files carry ``content`` (bytes) and have no children.  Directories
    carry ``children`` (name → VirtualEntry) and have no content.

    This is the single source of truth for the overlay that intercepts
    ``.readme/**`` reads on every backend inheriting ``ReadmeDocMixin``
    — the same tree drives ``read_content``, ``list_dir``,
    ``content_exists``, and ``get_content_size``.

    Issue #3728 decisions #5A (one declarative tree), #8A (sentinel
    returns for find/miss), #14A (single-walk generation).
    """

    name: str
    is_dir: bool
    content: bytes | None = None
    children: dict[str, "VirtualEntry"] = field(default_factory=dict)

    @property
    def is_file(self) -> bool:
        return not self.is_dir

    def size(self) -> int:
        """Return content size in bytes (0 for directories)."""
        return len(self.content) if self.content is not None else 0

    def find(self, parts: list[str]) -> "VirtualEntry | None":
        """Walk the tree to the entry at ``parts`` or return ``None``.

        Traversal stops and returns ``None`` as soon as a part doesn't
        exist or a non-directory is encountered mid-path. Does NOT
        normalize the input — callers must hand over clean parts
        (no ``..``, no empty strings, no leading/trailing slashes).
        """
        node: VirtualEntry | None = self
        for part in parts:
            if node is None or not node.is_dir:
                return None
            node = node.children.get(part)
        return node

    def list_children_names(self) -> list[str]:
        """Sorted child names with ``/`` suffix for sub-directories."""
        if not self.is_dir:
            return []
        return sorted(f"{name}/" if entry.is_dir else name for name, entry in self.children.items())


# Module-level cache keyed by (connector_class, mount_path).  Class objects
# are hashable and live for the process lifetime, so the cache is naturally
# scoped correctly: a hot-reloaded module produces a new class object and a
# new cache entry.  Decision #13A — one generation per (class, mount_path)
# per process.
_VIRTUAL_TREE_CACHE: dict[tuple[type, str], VirtualEntry] = {}


def _invalidate_virtual_tree_cache() -> None:
    """Test/dev helper: clear all cached virtual readme trees."""
    _VIRTUAL_TREE_CACHE.clear()


def _parse_readme_path_parts(
    backend_path: str | None,
    readme_dir: str = ".readme",
) -> list[str] | None:
    """Parse a backend-relative path into parts under ``.readme/``.

    Returns:
        - ``None`` if the path is NOT under ``.readme/`` (the caller should
          fall through to the normal read path — non-virtual).
        - ``[]`` if the path refers to the ``.readme/`` directory itself.
        - ``[part, ...]`` for nested entries.

    Raises ``ValueError`` on path traversal attempts (``..``, null bytes,
    backslashes).  Decision #4A — strict rejection so security issues are
    visible rather than silently falling through to the real backend.
    """
    if backend_path is None:
        return None

    # Reject null bytes and other control bytes that shouldn't be in paths
    if "\x00" in backend_path:
        raise ValueError("null byte in path")

    # Reject backslashes — Windows-style separators are not valid here
    if "\\" in backend_path:
        raise ValueError(f"backslash in path: {backend_path!r}")

    cleaned = backend_path.strip("/")
    if not cleaned:
        return None

    # posixpath.normpath collapses ``//``, ``.``, and ``..`` components.
    normalized = posixpath.normpath(cleaned)

    # After normalization, any path starting with ``..`` is a traversal attempt
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"path traversal detected: {backend_path!r}")

    readme_prefix = (readme_dir or ".readme").strip("/")
    if normalized == readme_prefix:
        return []

    prefix = readme_prefix + "/"
    if not normalized.startswith(prefix):
        return None

    # Split and drop any empty parts (defensive — normpath shouldn't produce them)
    parts = [p for p in normalized[len(prefix) :].split("/") if p]
    return parts


def _has_skill_name(backend: Any) -> bool:
    """Return True if the backend class declares a non-empty ``SKILL_NAME``."""
    return bool(getattr(type(backend), "SKILL_NAME", "") or "")


def _readme_dir_for(backend: Any) -> str:
    """Return the backend's configured ``.readme/`` directory name."""
    return getattr(type(backend), "README_DIR", ".readme") or ".readme"


def _defers_to_backend(backend: Any) -> bool:
    """Return True if the backend's real content should shadow the overlay.

    Issue #3728 finding #9: on writable path-addressed connectors (native
    gdrive), users can legitimately create real ``.readme/`` files — the
    overlay must not silently hide them.  Those connectors opt in by
    setting ``VIRTUAL_README_DEFERS_TO_BACKEND = True``.
    """
    return bool(getattr(type(backend), "VIRTUAL_README_DEFERS_TO_BACKEND", False))


def _real_content_exists(
    backend: Any,
    backend_path: str,
    context: Any | None = None,
) -> bool:
    """Best-effort probe: does the real backend have content at this path?

    Reuses the caller's context via ``_build_probe_context`` so
    user-scoped connectors (native Google Drive, etc.) can actually
    resolve an auth token and answer the question.

    Returns ``True`` only when the backend explicitly confirms existence.
    Any exception or missing probe method is treated as "does not exist"
    so the overlay still works on backends without a cheap exists check.
    """
    try:
        probe_ctx = _build_probe_context(context, backend_path)
        exists_fn = getattr(backend, "content_exists", None)
        if callable(exists_fn):
            return bool(exists_fn(backend_path, context=probe_ctx))
    except Exception:
        return False
    return False


def overlay_owns_path(
    backend: Any,
    mount_path: str,  # noqa: ARG001 — kept for API symmetry with dispatchers
    backend_path: str | None,
    context: Any | None = None,
) -> bool:
    """Return True if the virtual ``.readme/`` overlay authoritatively
    owns ``backend_path`` on this ``backend``.

    Single decision point used by every read/list/stat/exists/write
    code path (Issue #3728 round 5 refactor) — ensures reads, writes,
    and stats agree on who owns a given path.

    Returns False when:
    - Backend has no ``SKILL_NAME`` (overlay inactive)
    - ``backend_path`` is not under the configured readme directory
    - Backend opts to defer to real data (``VIRTUAL_README_DEFERS_TO_BACKEND``)
      AND the real ``.readme/`` directory exists on the backend
      (subtree-level ownership — Issue #3728 round 6 finding #16).
      The whole subtree is handed over to the backend atomically so
      listings, reads, writes, and stats cannot disagree about a
      "partly real, partly virtual" state.

    Propagates ``ValueError`` for malformed paths (traversal, null byte,
    backslash) so callers can raise a loud error rather than silently
    falling through to the real backend (Decision #4A).
    """
    if not _has_skill_name(backend):
        return False
    parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
    if parts is None:
        return False
    return not (_defers_to_backend(backend) and _real_readme_root_exists(backend, context=context))


def _build_probe_context(context: Any | None, backend_path: str) -> Any:
    """Build an OperationContext for backend probes (auth-preserving).

    When a caller context is supplied, reuse its ``user_id`` /
    ``zone_id`` / ``groups`` so user-scoped connectors can resolve an
    auth token.  Otherwise fall back to a synthetic system context.
    """
    from dataclasses import replace as _replace

    from nexus.contracts.types import OperationContext

    if context is None:
        return OperationContext(
            user_id="system",
            groups=[],
            is_system=True,
            backend_path=backend_path,
        )
    try:
        return _replace(context, backend_path=backend_path)
    except Exception:
        return OperationContext(
            user_id=getattr(context, "user_id", "system") or "system",
            groups=list(getattr(context, "groups", []) or []),
            zone_id=getattr(context, "zone_id", None),
            is_system=getattr(context, "is_system", False),
            backend_path=backend_path,
        )


def _real_readme_root_exists(backend: Any, context: Any | None = None) -> bool:
    """Probe the backend for a real ``.readme/`` directory (subtree root).

    **Error semantics (trade-off, round 8 finding #20):**
    Codex raised a concern that treating probe errors as "no real
    data" lets the overlay shadow a real ``.readme/`` during
    transient auth/network failures.  The inverse (fail-closed:
    treat errors as "real data exists, defer") breaks the much more
    common unauthed-mount case where users legitimately want to see
    the virtual docs before setting up OAuth — they have no real
    data to protect, and transient probes and "no OAuth configured"
    look the same from Python.

    We keep the original semantics — probe errors return ``False``
    so the overlay stays visible — and accept that on a fully
    authed deferring backend, a transient probe failure can briefly
    shadow real content until the probe recovers.  This trade is
    documented here so future reviewers can pick a different policy
    if the calculus changes.

    Probe order (first definite "yes" wins):
    1. ``backend.is_directory(readme_dir, context)`` — handles empty
       real directories that ``list_dir`` can't distinguish from
       "missing".
    2. ``backend.content_exists(readme_dir, context)``.
    3. Non-empty ``backend.list_dir(readme_dir, context)``.
    """
    readme_dir = _readme_dir_for(backend)
    probe_ctx = _build_probe_context(context, readme_dir)

    # 1) is_directory probe — handles the empty-directory case.
    is_dir_fn = getattr(backend, "is_directory", None)
    if callable(is_dir_fn):
        try:
            if is_dir_fn(readme_dir, context=probe_ctx):
                return True
        except Exception:
            pass

    # 2) content_exists probe.
    try:
        if _real_content_exists(backend, readme_dir, context=context):
            return True
    except Exception:
        pass

    # 3) list_dir probe (detects *non-empty* directories).
    list_fn = getattr(backend, "list_dir", None)
    if callable(list_fn):
        try:
            entries = list_fn(readme_dir, context=probe_ctx)
            if entries:
                return True
        except Exception:
            pass
    return False


def dispatch_virtual_readme_read(
    backend: Any,
    mount_path: str,
    backend_path: str | None,
    context: Any | None = None,
) -> bytes | None:
    """Serve a read from the virtual ``.readme/`` overlay if it matches.

    Returns:
        - ``bytes`` — the virtual file's content (hit).
        - ``None`` — path is NOT under ``.readme/`` (caller falls through
          to the real backend, decision #8A sentinel protocol).

    Raises:
        ``NexusFileNotFoundError`` — path IS under ``.readme/`` but no
          matching virtual entry exists (distinguishes "not virtual" from
          "virtual but missing", decision #8A).
        ``ValueError`` — path traversal or invalid input (decision #4A).
        ``IsADirectoryError`` — path matches a virtual directory, not a file.
        ``RuntimeError`` — backend has no ``SKILL_NAME``.  Safe default —
          non-skill backends return ``None`` from the ``_has_skill_name``
          check above, so this only fires for misconfigured skill backends.
    """
    if not overlay_owns_path(backend, mount_path, backend_path, context=context):
        return None

    parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
    # overlay_owns_path guarantees parts is not None here
    assert parts is not None

    from nexus.contracts.exceptions import NexusFileNotFoundError

    tree = get_virtual_readme_tree_for_backend(backend, mount_path)
    entry = tree.find(parts)
    if entry is None:
        raise NexusFileNotFoundError(f"virtual .readme/ path not found: {backend_path}")
    if entry.is_dir:
        raise IsADirectoryError(f"Is a directory: {backend_path}")
    return entry.content or b""


def dispatch_virtual_readme_list(
    backend: Any,
    mount_path: str,
    backend_path: str | None,
    context: Any | None = None,
) -> list[str] | None:
    """List a directory from the virtual ``.readme/`` overlay if it matches.

    Returns:
        - ``list[str]`` — the virtual directory's entries (hit).
        - ``None`` — path is NOT under ``.readme/``.

    Raises:
        ``NexusFileNotFoundError`` — path IS under ``.readme/`` but the
          directory doesn't exist.
        ``NotADirectoryError`` — path matches a virtual file, not a directory.
        ``ValueError`` — path traversal.
    """
    if not overlay_owns_path(backend, mount_path, backend_path, context=context):
        return None

    parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
    assert parts is not None

    from nexus.contracts.exceptions import NexusFileNotFoundError

    tree = get_virtual_readme_tree_for_backend(backend, mount_path)
    entry = tree.find(parts)
    if entry is None:
        raise NexusFileNotFoundError(f"virtual .readme/ path not found: {backend_path}")
    if not entry.is_dir:
        raise NotADirectoryError(f"Not a directory: {backend_path}")
    return entry.list_children_names()


def dispatch_virtual_readme_exists(
    backend: Any,
    mount_path: str,
    backend_path: str | None,
    context: Any | None = None,
) -> bool | None:
    """Answer ``content_exists`` from the virtual ``.readme/`` overlay.

    Returns:
        - ``True`` or ``False`` — definitive answer (path is under ``.readme/``).
        - ``None`` — path is NOT under ``.readme/``.
    """
    if not _has_skill_name(backend):
        return None

    try:
        parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
    except ValueError:
        # Malformed path under .readme/ — exists() is a predicate, not
        # an access attempt.  Return False so callers get a definitive
        # answer without a surprise exception.
        return False

    if parts is None:
        return None

    # Shared ownership check: fall through to real backend when the
    # overlay isn't authoritative here (e.g. native gdrive with a real
    # ``.readme/`` folder).
    try:
        _owns = overlay_owns_path(backend, mount_path, backend_path, context=context)
    except ValueError:
        return False
    if not _owns:
        return None

    try:
        tree = get_virtual_readme_tree_for_backend(backend, mount_path)
    except Exception:
        return False

    return tree.find(parts) is not None


def dispatch_virtual_readme_size(
    backend: Any,
    mount_path: str,
    backend_path: str | None,
    context: Any | None = None,
) -> int | None:
    """Return the virtual file size under ``.readme/``.

    Returns:
        - ``int`` — the file's size in bytes.
        - ``None`` — path is NOT under ``.readme/``.

    Raises ``NexusFileNotFoundError`` for virtual directories and for
    paths under ``.readme/`` that don't exist.
    """
    if not overlay_owns_path(backend, mount_path, backend_path, context=context):
        return None

    parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
    assert parts is not None

    from nexus.contracts.exceptions import NexusFileNotFoundError

    tree = get_virtual_readme_tree_for_backend(backend, mount_path)
    entry = tree.find(parts)
    if entry is None:
        raise NexusFileNotFoundError(f"virtual .readme/ path not found: {backend_path}")
    return entry.size()


def get_virtual_readme_tree_for_backend(backend: Any, mount_path: str) -> VirtualEntry:
    """Return the cached virtual ``.readme/`` tree for a backend instance.

    The tree is built once per ``(connector_class, mount_path)`` pair and
    reused across reads.  Backend must inherit from ``ReadmeDocMixin``
    (i.e., expose ``SKILL_NAME``, ``SCHEMAS``, ``OPERATION_TRAITS``,
    ``ERROR_REGISTRY``, ``EXAMPLES`` as class attributes).

    Raises ``RuntimeError`` if the backend doesn't declare a ``SKILL_NAME``
    (there's nothing to generate docs from).

    Issue #3728 decisions #13A (module cache), #14A (single walk per mount).
    """
    connector_class = type(backend)
    key: tuple[type, str] = (connector_class, mount_path)
    cached = _VIRTUAL_TREE_CACHE.get(key)
    if cached is not None:
        return cached

    skill_name = getattr(connector_class, "SKILL_NAME", "") or ""
    if not skill_name:
        raise RuntimeError(
            f"{connector_class.__name__} has no SKILL_NAME — cannot build virtual .readme/ tree"
        )

    # Pull class-level metadata.  We use the CLASS attributes (not instance
    # getattr) so the cache key — the class — fully determines the tree.
    schemas = dict(getattr(connector_class, "SCHEMAS", {}) or {})
    operation_traits = dict(getattr(connector_class, "OPERATION_TRAITS", {}) or {})
    error_registry = dict(getattr(connector_class, "ERROR_REGISTRY", {}) or {})
    examples = dict(getattr(connector_class, "EXAMPLES", {}) or {})
    nested_examples_raw = getattr(connector_class, "NESTED_EXAMPLES", None)
    field_examples_raw = getattr(connector_class, "FIELD_EXAMPLES", None)
    readme_dir = getattr(connector_class, "README_DIR", ".readme") or ".readme"

    # Extract write paths from CLIConnectorConfig if available (parallels the
    # existing logic in ``ReadmeDocMixin.get_doc_generator``).
    write_paths: dict[str, str] = {}
    _config = getattr(backend, "_config", None)
    if _config is not None and hasattr(_config, "write"):
        for wp in _config.write:
            write_paths[wp.operation] = wp.path

    generator = ReadmeDocGenerator(
        skill_name=skill_name,
        schemas=schemas,
        operation_traits=operation_traits,
        error_registry=error_registry,
        examples=examples,
        readme_dir=readme_dir,
        nested_examples=dict(nested_examples_raw) if nested_examples_raw else None,
        field_examples=dict(field_examples_raw) if field_examples_raw else None,
        write_paths=write_paths or None,
    )
    dir_structure = getattr(connector_class, "DIRECTORY_STRUCTURE", None)
    if dir_structure:
        generator._directory_structure = dir_structure

    tree = generator.generate_tree(mount_path)

    # IMPORTANT (Issue #3728): connectors can override ``generate_readme``
    # to substitute a curated static markdown file (see
    # ``PathGmailBackend.generate_readme`` which loads
    # ``connectors/gmail/README.md`` with the authoritative ``SENT/_reply.yaml``
    # / ``SENT/_forward.yaml`` write paths).  The generator-built README in
    # ``tree`` only uses class-level metadata and can't see those overrides,
    # so we ask the backend itself for the README.md body and overwrite the
    # generator's version.  ``schemas/`` and ``examples/`` still come from
    # the generator — those don't have connector-level overrides today.
    try:
        backend_readme = backend.generate_readme(mount_path)
    except Exception:
        backend_readme = None
    if isinstance(backend_readme, str) and backend_readme:
        tree.children["README.md"] = VirtualEntry(
            name="README.md",
            is_dir=False,
            content=backend_readme.encode("utf-8"),
        )

    _VIRTUAL_TREE_CACHE[key] = tree
    return tree


class ReadmeDocGenerator:
    """Generate README.md documentation from connector metadata.

    Parameters
    ----------
    skill_name:
        Skill identifier (e.g., ``"gcalendar"``).
    schemas:
        Operation name → Pydantic model mapping.
    operation_traits:
        Operation name → OpTraits mapping.
    error_registry:
        Error code → ErrorDef mapping.
    examples:
        Example files: ``{"create_meeting.yaml": "content..."}``.
    readme_dir:
        Directory name for readme docs (default: ``".readme"``).
    nested_examples:
        Configurable nested-field examples (overrides defaults).
    """

    def __init__(
        self,
        skill_name: str,
        schemas: dict[str, type[BaseModel]],
        operation_traits: dict[str, OpTraits],
        error_registry: dict[str, ErrorDef],
        examples: dict[str, str],
        readme_dir: str = ".readme",
        nested_examples: dict[str, list[str]] | None = None,
        field_examples: dict[str, str] | None = None,
        write_paths: dict[str, str] | None = None,
    ) -> None:
        self._skill_name = skill_name
        self._schemas = schemas
        self._operation_traits = operation_traits
        self._error_registry = error_registry
        self._examples = examples
        self._readme_dir = readme_dir
        self._nested_examples = nested_examples or {}
        self._field_examples = field_examples or {}
        # operation_name -> write path (e.g., "send_email" -> "SENT/_new.yaml")
        self._write_paths = write_paths or {}
        # Optional directory structure description (set by connector)
        self._directory_structure: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_readme(self, mount_path: str) -> str:
        """Auto-generate README.md from connector metadata.

        Args:
            mount_path: The mount path for this connector.

        Returns:
            Generated README.md content as string.
        """
        lines = [
            f"# {self._format_display_name()} Connector",
            "",
            "## Mount Path",
            f"`{mount_path}`",
            "",
        ]

        # Directory structure (if provided)
        if self._directory_structure:
            lines.extend(
                ["## Directory Structure", "", "```", self._directory_structure, "```", ""]
            )

        # Read patterns + write operations (Issue #3148)
        lines.extend(self._generate_read_patterns_section(mount_path))

        if self._operation_traits:
            lines.extend(self._generate_required_format_section())

        if self._error_registry:
            lines.extend(self._generate_errors_section())

        return "\n".join(lines)

    def get_readme_path(self, mount_path: str) -> str:
        """Get the full path to the .readme directory."""
        return posixpath.join(mount_path.rstrip("/"), self._readme_dir)

    def generate_tree(self, mount_path: str) -> VirtualEntry:
        """Build the full virtual ``.readme/`` tree in one metadata walk.

        Issue #3728 decision #14A — one pass over ``SCHEMAS`` and
        ``EXAMPLES`` produces every file in the virtual tree, eliminating
        the N+1 pattern of reading schemas one at a time.

        Returns a ``VirtualEntry`` rooted at the ``.readme/`` directory
        containing:
        - ``README.md`` — full generated markdown from ``generate_readme``
        - ``schemas/<op>.yaml`` — one annotated schema per operation
        - ``examples/<filename>`` — one entry per example (bytes-preserving)

        Errors from the generator are propagated — decision #8A, the
        caller decides how to surface them.
        """
        root = VirtualEntry(name=self._readme_dir, is_dir=True)

        # README.md (reuses the existing markdown pipeline).
        readme_text = self.generate_readme(mount_path)
        root.children["README.md"] = VirtualEntry(
            name="README.md",
            is_dir=False,
            content=readme_text.encode("utf-8"),
        )

        # schemas/<op>.yaml — one annotated schema per operation.
        if self._schemas:
            schemas_dir = VirtualEntry(name="schemas", is_dir=True)
            for op_name, schema in self._schemas.items():
                yaml_text = self.generate_schema_yaml(op_name, schema)
                file_name = f"{op_name}.yaml"
                schemas_dir.children[file_name] = VirtualEntry(
                    name=file_name,
                    is_dir=False,
                    content=yaml_text.encode("utf-8"),
                )
            root.children["schemas"] = schemas_dir

        # examples/<filename> — bytes-preserving (#7A binary examples).
        if self._examples:
            examples_dir = VirtualEntry(name="examples", is_dir=True)
            for filename, raw in self._examples.items():
                if isinstance(raw, bytes):
                    content_bytes = raw
                elif isinstance(raw, str):
                    content_bytes = raw.encode("utf-8")
                else:
                    content_bytes = str(raw).encode("utf-8")
                examples_dir.children[filename] = VirtualEntry(
                    name=filename,
                    is_dir=False,
                    content=content_bytes,
                )
            root.children["examples"] = examples_dir

        return root

    # NOTE (Issue #3728): ``write_readme`` was removed. Skill docs are now
    # served on-demand by the virtual ``.readme/`` overlay via
    # ``generate_tree`` + ``dispatch_virtual_readme_*``. Materializing files
    # into the storage layer would drift from class metadata and double
    # the code paths that produce docs (decision #2A: virtual-only).

    def _generate_read_patterns_section(self, mount_path: str) -> list[str]:
        """Generate Read Patterns section showing how to list, cat, grep content.

        Provides agents with L0-L1 discovery: how to explore connector content
        before attempting write operations. Issue #3148.
        """
        mp = mount_path.rstrip("/")
        lines = [
            "## Read Patterns",
            "",
            "### List content",
            "```bash",
            f"nexus ls {mp}/",
            "```",
            "",
            "### Read a file",
            "```bash",
            f"nexus cat {mp}/<path>",
            "```",
            "",
            "### Search content",
            "```bash",
            f'nexus grep "keyword" {mp}/',
            "```",
            "",
        ]

        # Add write operations with exact paths and inline schemas
        if self._schemas:
            lines.extend(["## Operations", ""])

            for op_name, schema in self._schemas.items():
                traits = self._operation_traits.get(op_name, OpTraits())
                display = op_name.replace("_", " ").title()
                write_path = self._write_paths.get(op_name, "_new.yaml")

                lines.append(f"### {display}")
                lines.append("")
                lines.append(f"Write to `{mp}/{write_path}`:")
                lines.append(f"- Reversibility: **{traits.reversibility.value}**")
                lines.append(f"- Confirm: **{traits.confirm.value}**")

                if traits.confirm == ConfirmLevel.USER:
                    lines.append("- **⚠ IRREVERSIBLE** — requires `user_confirmed: true`")

                lines.append("")
                lines.append("```yaml")

                if traits.confirm >= ConfirmLevel.INTENT:
                    lines.append("# agent_intent: <why you are doing this — min 10 chars>")
                if traits.confirm >= ConfirmLevel.EXPLICIT:
                    lines.append("# confirm: true")
                if traits.confirm == ConfirmLevel.USER:
                    lines.append("# user_confirmed: true  # ask user first")

                # Inline schema fields
                for field_name, field_info in schema.model_fields.items():
                    if field_name in ("agent_intent", "confirm", "user_confirmed"):
                        continue
                    required = field_info.is_required()
                    req_tag = "REQUIRED" if required else "optional"
                    desc = field_info.description or ""
                    example = self._get_field_example(
                        field_name, field_info, field_info.annotation, required
                    )
                    lines.append(
                        f"{field_name}: {example}  # {req_tag}{' — ' + desc if desc else ''}"
                    )

                lines.append("```")
                lines.append("")

                for warning in traits.warnings:
                    lines.append(f"> **Warning:** {warning}")
                    lines.append("")

            lines.append("")

        # Add schema discovery
        lines.extend(
            [
                "### Schema discovery",
                "```bash",
                f"nexus mounts skills {mp}",
                f"nexus mounts schema {mp} <operation>",
                "```",
                "",
            ]
        )

        return lines

    def generate_schema_yaml(self, op_name: str, schema: type[BaseModel]) -> str:
        """Generate an annotated YAML schema file for a single operation.

        Each field includes type, required/optional, constraints, and description
        from Pydantic field metadata. This is the L2 discovery layer that agents
        use to construct valid writes.

        Args:
            op_name: Operation name (e.g., "send_email").
            schema: Pydantic model class.

        Returns:
            Annotated YAML content as string.
        """
        traits = self._operation_traits.get(op_name, OpTraits())
        lines = [
            f"# Schema: {op_name}",
            f"# Connector: {self._format_display_name()}",
            f"# Reversibility: {traits.reversibility.value}",
            f"# Confirm level: {traits.confirm.value}",
            "#",
        ]

        if traits.confirm >= ConfirmLevel.INTENT:
            lines.append("# agent_intent: <required, min 10 chars — why you are doing this>")
        if traits.confirm >= ConfirmLevel.EXPLICIT:
            lines.append("# confirm: true  # REQUIRED")

        lines.append("")

        for field_name, field_info in schema.model_fields.items():
            if field_name in ("agent_intent", "confirm", "user_confirmed"):
                continue

            annotation = field_info.annotation
            required = field_info.is_required()
            description = field_info.description or ""
            req_label = "required" if required else "optional"

            # Type name
            type_name = self._get_type_name(annotation)

            # Constraints from metadata
            constraints = self._get_field_constraints(field_info)
            constraint_str = f", {constraints}" if constraints else ""

            comment = f"# {req_label}, {type_name}{constraint_str}"
            if description:
                comment += f" — {description}"

            lines.append(comment)

            example = self._get_field_example(field_name, field_info, annotation, required)
            lines.append(f"{field_name}: {example}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _get_type_name(annotation: Any) -> str:
        """Get human-readable type name from annotation."""
        if annotation is None:
            return "any"
        origin = getattr(annotation, "__origin__", None)
        if origin is list:
            args = getattr(annotation, "__args__", ())
            inner = args[0].__name__ if args else "any"
            return f"list[{inner}]"
        if origin is dict:
            return "dict"
        if hasattr(annotation, "__name__"):
            return str(annotation.__name__)
        return str(annotation)

    @staticmethod
    def _get_field_constraints(field_info: Any) -> str:
        """Extract constraint string from Pydantic field metadata."""
        parts = []
        for meta in field_info.metadata or []:
            if hasattr(meta, "min_length") and meta.min_length is not None:
                parts.append(f"min_length={meta.min_length}")
            if hasattr(meta, "max_length") and meta.max_length is not None:
                parts.append(f"max_length={meta.max_length}")
            if hasattr(meta, "ge") and meta.ge is not None:
                parts.append(f"min={meta.ge}")
            if hasattr(meta, "le") and meta.le is not None:
                parts.append(f"max={meta.le}")
            if hasattr(meta, "pattern") and meta.pattern is not None:
                parts.append(f"pattern={meta.pattern}")
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Section generators
    # ------------------------------------------------------------------

    def _generate_operations_section(self) -> list[str]:
        """Generate Operations section from SCHEMAS."""
        lines = ["## Operations", ""]

        for op_name, schema in self._schemas.items():
            display_name = op_name.replace("_", " ").title()
            lines.append(f"### {display_name}")
            lines.append("")

            traits = self._operation_traits.get(op_name, OpTraits())

            lines.append("```yaml")

            if traits.confirm >= ConfirmLevel.INTENT:
                lines.append("# agent_intent: <reason for this operation>")

            if traits.confirm >= ConfirmLevel.EXPLICIT:
                lines.append("# confirm: true")

            lines.extend(self._schema_to_yaml_lines(schema))
            lines.append("```")
            lines.append("")

            for warning in traits.warnings:
                lines.append(f"> **Warning:** {warning}")
                lines.append("")

        return lines

    def _generate_required_format_section(self) -> list[str]:
        """Generate Required Format section from OPERATION_TRAITS."""
        lines = ["## Required Format", ""]

        intent_ops = []
        explicit_ops = []
        user_ops = []

        for op_name, traits in self._operation_traits.items():
            if traits.confirm == ConfirmLevel.USER:
                user_ops.append(op_name)
            elif traits.confirm == ConfirmLevel.EXPLICIT:
                explicit_ops.append(op_name)
            elif traits.confirm == ConfirmLevel.INTENT:
                intent_ops.append(op_name)

        if intent_ops or explicit_ops or user_ops:
            lines.append("All operations require `# agent_intent: <reason>` as the first line.")
            lines.append("")

        if explicit_ops:
            ops_str = ", ".join(f"`{op}`" for op in explicit_ops)
            lines.append(f"Operations requiring explicit confirmation ({ops_str}):")
            lines.append("- Add `# confirm: true` after agent_intent")
            lines.append("")

        if user_ops:
            ops_str = ", ".join(f"`{op}`" for op in user_ops)
            lines.append(f"Operations requiring user confirmation ({ops_str}):")
            lines.append("- Add `# user_confirmed: true` after getting explicit user approval")
            lines.append("- **These operations CANNOT be undone**")
            lines.append("")

        return lines

    def _generate_errors_section(self) -> list[str]:
        """Generate Error Codes section from ERROR_REGISTRY."""
        lines = ["## Error Codes", ""]

        for code, error_def in self._error_registry.items():
            lines.append(f"### {code}")
            lines.append(error_def.message)
            lines.append("")

            if error_def.fix_example:
                lines.append("**Fix:**")
                lines.append("```yaml")
                lines.append(error_def.fix_example)
                lines.append("```")
                lines.append("")

        return lines

    # ------------------------------------------------------------------
    # Schema → YAML helpers
    # ------------------------------------------------------------------

    def _schema_to_yaml_lines(self, schema: type[BaseModel]) -> list[str]:
        """Convert Pydantic schema to YAML example lines."""
        lines = []

        for field_name, field_info in schema.model_fields.items():
            if field_name in ("agent_intent", "confirm"):
                continue

            annotation = field_info.annotation
            required = field_info.is_required()
            default = field_info.default

            example = self._get_field_example(field_name, field_info, annotation, required)

            if self._is_nested_model(annotation):
                lines.append(f"{field_name}:")
                nested_lines = self._get_nested_example(field_name, annotation, required)
                lines.extend(f"  {line}" for line in nested_lines)
            elif default is not None and str(default) not in ("PydanticUndefined", "..."):
                if isinstance(default, bool):
                    lines.append(f"{field_name}: {str(default).lower()}")
                elif isinstance(default, list):
                    lines.append(f"{field_name}: []")
                else:
                    lines.append(f"{field_name}: {default}")
            else:
                lines.append(f"{field_name}: {example}")

        return lines

    def _is_nested_model(self, annotation: Any) -> bool:
        """Check if annotation is a nested Pydantic model."""
        try:
            import types

            if isinstance(annotation, types.UnionType):
                args = getattr(annotation, "__args__", ())
                return any(arg is not type(None) and hasattr(arg, "model_fields") for arg in args)

            origin = getattr(annotation, "__origin__", None)
            if origin is type(None) or str(origin) == "typing.Union":
                args = getattr(annotation, "__args__", ())
                for arg in args:
                    if arg is not type(None) and hasattr(arg, "model_fields"):
                        return True
            return hasattr(annotation, "model_fields")
        except Exception:
            return False

    def _get_nested_example(self, field_name: str, _annotation: Any, required: bool) -> list[str]:
        """Get example lines for nested model."""
        if field_name in self._nested_examples:
            return list(self._nested_examples[field_name])

        suffix = ", required" if required else ", optional"
        return [f"# <nested object{suffix}>"]

    def _get_field_example(
        self, field_name: str, _field_info: Any, annotation: Any, required: bool
    ) -> str:
        """Get example value for a field.

        Checks connector-provided ``field_examples`` first, then falls
        back to generic type-based placeholders.
        """
        if field_name in self._field_examples:
            return self._field_examples[field_name]

        type_hint = self._format_type_hint(annotation)
        suffix = ", required" if required else ", optional"

        if "list" in type_hint.lower():
            return "[]"
        elif "bool" in type_hint.lower():
            return "true"
        elif "int" in type_hint.lower():
            return "0"

        return f"<{type_hint}{suffix}>"

    def _format_type_hint(self, annotation: Any) -> str:
        """Format type annotation as readable string."""
        if annotation is None:
            return "any"

        type_name = getattr(annotation, "__name__", str(annotation))

        if "str" in type_name.lower():
            return "string"
        elif "int" in type_name.lower():
            return "integer"
        elif "bool" in type_name.lower():
            return "boolean"
        elif "list" in type_name.lower():
            return "list"
        elif "dict" in type_name.lower():
            return "object"

        return type_name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_display_name(self) -> str:
        """Format skill_name as display name."""
        return self._skill_name.replace("_", " ").replace("-", " ").title()
