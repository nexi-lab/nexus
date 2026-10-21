"""Explicit operator build preparation; no untrusted build commands are executed."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from nexus.contracts.agent_hosted import HostedImage
from nexus.services.product_agent_registry.dify_import import compile_dify_text

BASE = "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def prepare(
    source: Path,
    profile: str,
    runtime_files: Path,
    output: Path,
    model_resource: str,
    model_name: str,
):
    raw = source.read_bytes()
    if not raw or len(raw) > 256000:
        raise ValueError("HOSTED_SOURCE_LIMIT")
    plan = compile_dify_text(raw) if profile == "dify-0.6.0-linear-text-v1" else None
    if plan and plan.source_model != model_name:
        raise ValueError("HOSTED_SOURCE_MODEL_MISMATCH")
    lock = (runtime_files / "requirements.lock").read_bytes()
    descriptor = HostedImage(
        profile=profile,
        image_id="sha256:" + "0" * 64,
        source_digest=digest(raw),
        dependency_lock_digest=digest(lock),
        model_resource=model_resource,
        model_name=model_name,
        max_output_tokens=(plan.max_tokens or 128) if plan else 128,
    )
    # The output must be new. Builds cannot overwrite an existing reviewed context.
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(runtime_files / "runner.py", output / "runner.py")
    (output / "requirements.lock").write_bytes(lock)
    if plan:
        (output / "source.yml").write_bytes(raw)
        config = {"profile": profile, "plan": plan.model_dump()}
    else:
        # A reviewed Python entry module exposing build_graph(model, trace).
        # Do not import or execute the module on the management host.
        (output / "project.py").write_bytes(raw)
        config = {"profile": profile}
    (output / "profile.json").write_text(json.dumps(config, ensure_ascii=False) + "\n")
    labels = {
        "dev.sudo.agent.protocol": descriptor.protocol,
        "dev.sudo.agent.source": descriptor.source_digest,
        "dev.sudo.agent.dependencies": descriptor.dependency_lock_digest,
        "dev.sudo.agent.profile": descriptor.profile,
    }
    dockerfile = (
        f"FROM {BASE}\n"
        "COPY wheelhouse /wheelhouse\nCOPY requirements.lock /requirements.lock\n"
        "RUN python -m pip install --no-cache-dir --no-index --find-links=/wheelhouse --require-hashes -r /requirements.lock && rm -rf /wheelhouse\n"
        "WORKDIR /app\nCOPY runner.py profile.json /app/\n"
        + ("COPY source.yml /app/\n" if plan else "COPY project.py /app/\n")
        + "\n".join(f"LABEL {name}={json.dumps(value)}" for name, value in labels.items())
        + '\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nUSER 65532:65532\nENTRYPOINT ["python", "/app/runner.py"]\n'
    )
    (output / "Dockerfile").write_text(dockerfile)
    # Deliberately not a valid import descriptor until actual image inspect supplies its ID.
    draft = descriptor.model_dump(exclude={"image_id"})
    (output / "descriptor-draft.json").write_text(json.dumps(draft, indent=2) + "\n")
    return draft


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--profile", choices=["dify-0.6.0-linear-text-v1", "langgraph-stdio-v1"], required=True
    )
    parser.add_argument("--runtime-files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-resource", required=True)
    parser.add_argument("--model-name", required=True)
    args = parser.parse_args()
    prepare(
        args.source,
        args.profile,
        args.runtime_files,
        args.output,
        args.model_resource,
        args.model_name,
    )
    print(
        "Prepared build context; add verified wheelhouse, build offline, then record actual image ID."
    )


if __name__ == "__main__":
    main()
