#!/usr/bin/env python3
"""Enforce that every cdylib in the workspace has a signed-release workflow.

The kernel's `PluginLoader::load` (in nexus-vfs) rejects any `.so/.dylib/.dll`
without a valid Ed25519 `.sig` alongside it. So shipping a cdylib without a
release workflow that signs it produces a binary the cluster will refuse to
load at runtime — a regression that's invisible at PR time and only
surfaces when an operator tries to install the plugin.

This script catches that miss at PR time by walking the workspace, finding
every `crate-type = [\"cdylib\"]` package, and asserting one of:

  (a) A workflow under `.github/workflows/` invokes the reusable release
      pipeline with `plugin_name: <package-name>`, OR
  (b) A workflow under `.github/workflows/` runs `scripts/sign_plugin.py`
      AND mentions the package name in the same file (legacy / custom
      signing flow).

Either way, the cdylib has a path to ship signed. Anything else fails.

Run locally:  python scripts/check_plugin_signing_compliance.py
CI:           .github/workflows/plugin-signing-compliance.yml

Allow-list:   add a name to `_NON_PLUGIN_CDYLIBS` only with a code comment
              explaining why the cdylib will never be loaded by cluster
              (e.g. Python extension, FFI binding for an unrelated host).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# cdylibs that are intentionally NOT cluster plugins — they will never be
# loaded by `nexusd-cluster --plugin-dir` and so don't need a signed
# release workflow. Adding entries here is allowed but rare; always
# document the reason inline.
_NON_PLUGIN_CDYLIBS: set[str] = set()

# Regexes are intentionally loose — Cargo.toml authors may write crate-type
# across lines or with different whitespace. Match anything that pairs
# `crate-type` with `cdylib`.
_CDYLIB_RE = re.compile(
    r"""crate-type\s*=\s*\[[^\]]*['"]cdylib['"][^\]]*\]""",
    re.DOTALL,
)
_PACKAGE_NAME_RE = re.compile(
    r"""^\s*name\s*=\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def find_cdylib_packages(root: Path) -> list[tuple[str, Path]]:
    """Walk the workspace and return [(package_name, cargo_toml_path)] for cdylibs."""
    result: list[tuple[str, Path]] = []
    for cargo_toml in (root / "rust").rglob("Cargo.toml"):
        text = cargo_toml.read_text(encoding="utf-8")
        if not _CDYLIB_RE.search(text):
            continue
        # Extract `name = "..."` from the `[package]` table. The regex
        # matches the first one; in practice every Cargo.toml has exactly
        # one `[package] name = `.
        m = _PACKAGE_NAME_RE.search(text)
        if not m:
            raise SystemExit(
                f"cdylib at {cargo_toml.relative_to(root)} has no [package] name = '...'"
            )
        result.append((m.group(1), cargo_toml))
    result.sort()
    return result


# Detect a workflow that exists only to be called from another workflow
# (reusable infra). These never sign anything themselves — their callers
# do. Excluded from the scan so a reusable that *describes* multiple
# plugins in its input docstring doesn't get credited as signing them.
_REUSABLE_TRIGGER_RE = re.compile(r"^on:\s*\n\s*workflow_call\s*:", re.MULTILINE)


def find_release_workflows(root: Path) -> list[tuple[Path, str]]:
    """Return [(workflow_path, full_text)] for every leaf release workflow.

    Skips reusable workflows (`on: workflow_call`) — those aren't release
    pipelines themselves, they're shared infra.
    """
    out: list[tuple[Path, str]] = []
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return out
    for wf in sorted(wf_dir.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if _REUSABLE_TRIGGER_RE.search(text):
            continue
        out.append((wf, text))
    return out


def has_signing_path(
    package_name: str,
    workflows: list[tuple[Path, str]],
) -> tuple[bool, str | None]:
    """Does any workflow sign this package? Returns (ok, hint_workflow_path)."""
    # Pattern (a): leaf workflow calls the reusable with `plugin_name: <name>`
    # in its `with:` block. The plugin_name line must appear on its own at
    # `with:`-block indentation (>=6 spaces), unquoted or single-line —
    # description prose mentioning the package name doesn't satisfy this.
    reusable_marker = "release-plugin-reusable.yml"
    plugin_name_kv = re.compile(
        rf"""^\s{{6,}}plugin_name\s*:\s*['"]?{re.escape(package_name)}['"]?\s*$""",
        re.MULTILINE,
    )
    for wf, text in workflows:
        if reusable_marker in text and plugin_name_kv.search(text):
            return True, str(wf.name)

    # Pattern (b): direct sign_plugin.py call that mentions the package.
    # Looser — catches one-off signing flows that pre-date the reusable.
    sign_marker = "scripts/sign_plugin.py"
    name_marker = package_name
    for wf, text in workflows:
        if sign_marker in text and name_marker in text:
            return True, str(wf.name)

    return False, None


def check_trust_root_mirror(root: Path) -> tuple[bool, list[str]]:
    """Verify each `data/vault-signing/pubkeys/*.pub` has a matching
    `include_bytes!` entry under `TRUSTED_KEY_FILES` in nexus-vfs's
    `rust/kernel/src/kernel/plugins/loader.rs` at the workspace-pinned
    rev.

    A signed plugin only loads if the kernel embeds the matching
    verifying key. The keystore here and the embedded set in nexus-vfs
    are two halves of the same SSOT — drift = silent dlopen rejection
    at runtime. Fail-soft on fetch errors; transient network issues
    shouldn't take down a CI gate.

    Returns (ok, messages).
    """
    pubkeys_dir = root / "data" / "vault-signing" / "pubkeys"
    if not pubkeys_dir.is_dir():
        return True, ["[SKIP] no data/vault-signing/pubkeys/; mirror check has nothing to do"]

    local_names = sorted(p.stem for p in pubkeys_dir.glob("*.pub"))
    if not local_names:
        return True, ["[SKIP] no pubkeys/*.pub locally; mirror check has nothing to do"]

    cargo = root / "Cargo.toml"
    text = cargo.read_text(encoding="utf-8")
    m = re.search(r'nexus-plugin-abi[^\n]*rev\s*=\s*"([a-f0-9]{40})"', text)
    if not m:
        return True, [
            "[SKIP] cannot parse nexus-vfs rev from Cargo.toml; mirror check inconclusive"
        ]
    rev = m.group(1)

    url = (
        f"https://raw.githubusercontent.com/nexi-lab/nexus-vfs/{rev}"
        "/rust/kernel/src/kernel/plugins/loader.rs"
    )
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(url, timeout=15) as resp:
            loader_text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return True, [f"[SKIP] fetch of nexus-vfs loader.rs failed ({exc}); fail-soft per design"]

    # `include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/trusted_keys/<name>.pub"))`
    # The path is the only place the pubkey name appears in source — match it.
    referenced = set(re.findall(r'/trusted_keys/([^"]+)\.pub"', loader_text))
    messages: list[str] = []
    missing: list[str] = []
    for name in local_names:
        if name in referenced:
            messages.append(f"[ OK ] pubkey '{name}.pub' mirrored in nexus-vfs@{rev[:7]}")
        else:
            missing.append(name)
            messages.append(
                f"[FAIL] pubkey '{name}.pub' NOT in nexus-vfs@{rev[:7]} TRUSTED_KEY_FILES\n"
                f"       Drop the same file under nexus-vfs/rust/kernel/trusted_keys/\n"
                f"       and append an include_bytes! entry to TRUSTED_KEY_FILES, then bump\n"
                f"       this repo's Cargo.toml pin to a rev that contains both edits."
            )
    return not missing, messages


def main() -> int:
    root = _repo_root()
    cdylibs = find_cdylib_packages(root)
    workflows = find_release_workflows(root)

    failures: list[str] = []
    for name, cargo_toml in cdylibs:
        if name in _NON_PLUGIN_CDYLIBS:
            print(f"[SKIP] {name} — allow-listed in _NON_PLUGIN_CDYLIBS")
            continue
        ok, hint = has_signing_path(name, workflows)
        rel = cargo_toml.relative_to(root)
        if ok:
            print(f"[ OK ] {name} ({rel}) — signed by {hint}")
        else:
            failures.append(
                f"  - {name} (at {rel})\n"
                f"    No release workflow signs this dylib. Add one of:\n"
                f"    (a) a workflow that `uses: ./.github/workflows/release-plugin-reusable.yml`\n"
                f"        with `plugin_name: {name}` and `signing_key_source: vault`, OR\n"
                f"    (b) a workflow that runs `scripts/sign_plugin.py` against this dylib\n"
                f"        (legacy path — prefer the reusable).\n"
                f"    Cluster's PluginLoader::load rejects unsigned dylibs at boot.\n"
            )

    print()
    mirror_ok, mirror_msgs = check_trust_root_mirror(root)
    for line in mirror_msgs:
        print(line)

    if failures:
        sys.stderr.write(
            "Plugin signing compliance check FAILED.\n"
            "The following cdylibs ship without a signed release workflow:\n\n"
        )
        for line in failures:
            sys.stderr.write(line + "\n")
        return 1

    if not mirror_ok:
        sys.stderr.write(
            "Plugin signing compliance check FAILED.\n"
            "One or more pubkeys lack a matching entry in the nexus-vfs trust root.\n"
        )
        return 1

    print(f"\nAll {len(cdylibs)} cdylib(s) have a signing path; trust-root mirror is in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
