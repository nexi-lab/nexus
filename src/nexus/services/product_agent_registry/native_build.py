"""Prepare a Dify-to-SudoCode build candidate; this is not L3 admission or publication.

Only one system prompt followed by one independent text input is supported.
The original Dify source is compiled for validation, not executed in the target.
Unsupported roles/graphs/permissions fail before creating the output directory.
"""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

from nexus.contracts.agent_native import NATIVE_PROFILE, NativeDefinition, NativeRuntime
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.dify_import import compile_dify_text
from nexus.services.product_agent_registry.hosted_build import BASE, digest

PROFILE = NATIVE_PROFILE


def prepare(source: Path, executable: Path, runtime_commit: str, runner: Path, output: Path):
    if not re.fullmatch(r"[0-9a-f]{40}", runtime_commit):
        raise ValueError("NATIVE_RUNTIME_COMMIT_REQUIRED")
    with source.open("rb") as handle:
        raw = handle.read(256001)
    plan = compile_dify_text(raw)
    if (
        len(plan.prompts) != 1
        or plan.prompts[0].role != "system"
        or not plan.prompts[0].content.strip()
    ):
        raise ValueError("NATIVE_SOURCE_PROMPT_PROFILE_UNSUPPORTED")
    size = executable.stat().st_size
    if not 64 <= size <= 512 * 1024 * 1024:
        raise ValueError("NATIVE_EXECUTABLE_SIZE")
    with executable.open("rb") as handle:
        header = handle.read(64)
        # A Linux target is required. A macOS feasibility binary must not be
        # packaged as if it could execute inside the deployment container.
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] not in (b">\x00", b"\xb7\x00"):
            raise ValueError("NATIVE_LINUX_EXECUTABLE_REQUIRED")
        handle.seek(0)
        executable_digest = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    runner_bytes = runner.read_bytes()
    if not 1 <= len(runner_bytes) <= 128000:
        raise ValueError("NATIVE_RUNNER_SIZE")
    runtime = NativeRuntime(
        commit=runtime_commit,
        executable_digest=executable_digest,
        runner_digest=digest(runner_bytes),
        platform="linux/amd64" if header[18:20] == b">\x00" else "linux/arm64",
    ).model_dump()
    configuration = NativeDefinition(plan=plan).model_dump()
    lock = canonical_bytes({"runtime": runtime, "base_image": BASE})
    draft = {
        "kind": "NativeBuildDraft",
        "profile": PROFILE,
        "source_digest": plan.source_digest,
        "definition_digest": digest(canonical_bytes(configuration)),
        "dependency_lock_digest": digest(lock),
        "runtime": runtime,
        "evidence": "build_preparation_only",
        "constraints": {
            "conversation_mode": "independent_turns",
            "tools": [],
            "memory": "disabled",
            "input": "literal_text",
            "max_model_calls": 1,
        },
    }
    output.mkdir(parents=True, exist_ok=False)
    # There is intentionally no valid registered artifact or grade assertion in
    # this directory. Build/inspect/qualification and owner admission follow.
    (output / "profile.json").write_bytes(canonical_bytes(configuration))
    (output / "runtime.lock.json").write_bytes(lock)
    (output / "source.yml").write_bytes(raw)
    (output / "runner.py").write_bytes(runner_bytes)
    shutil.copyfile(executable, output / "scode")
    (output / "scode").chmod(0o555)
    with (output / "scode").open("rb") as handle:
        if "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest() != executable_digest:
            raise ValueError("NATIVE_EXECUTABLE_CHANGED_DURING_PREPARATION")
    labels = {
        "dev.sudo.agent.protocol": "agent-hosted-stdio/v1",
        "dev.sudo.agent.profile": PROFILE,
        "dev.sudo.agent.source": plan.source_digest,
        "dev.sudo.agent.dependencies": digest(lock),
        "dev.sudo.agent.native-definition": draft["definition_digest"],
        "dev.sudo.agent.native-runtime": executable_digest,
        "dev.sudo.agent.native-runner": runtime["runner_digest"],
        "dev.sudo.agent.native-commit": runtime_commit,
        "dev.sudo.agent.native-platform": runtime["platform"],
    }
    dockerfile = f"FROM {BASE}\nWORKDIR /app\nCOPY scode runner.py profile.json /app/\n"
    dockerfile += "\n".join(f"LABEL {key}={json.dumps(value)}" for key, value in labels.items())
    dockerfile += '\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nUSER 65532:65532\nENTRYPOINT ["python", "/app/runner.py"]\n'
    (output / "Dockerfile").write_text(dockerfile)
    (output / "native-draft.json").write_bytes(canonical_bytes(draft))
    return draft


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("source", "executable", "runner", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--runtime-commit", required=True)
    args = parser.parse_args()
    prepare(args.source, args.executable, args.runtime_commit, args.runner, args.output)
    print(
        "Prepared native build candidate. No L3 admission, registration or publication performed."
    )


if __name__ == "__main__":
    main()
