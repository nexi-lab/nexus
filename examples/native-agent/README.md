# Independent-text native build candidate

This builder converts the supported Dify source definition into a SudoCode
runtime configuration. The target does not load Dify or LangGraph. It runs the
native SudoCode engine with tools and automatic memory disabled, accepting
literal text. Unsupported source roles, nodes, tools, memory, and files are
rejected; they are never silently flattened into a prompt.

The builder produces a candidate, **not L3 qualification or publication**.
Registry admission requires the exact native profile, runtime and source binding,
a successful stored migration comparison, execution verification, independent
attestation and a separate IT review. Registration alone never grants L3.

## Prepare

Use a reviewed Linux SudoCode executable containing `--no-tools`, `--no-memory`,
and `--literal-input`. Supply its exact source commit and the original Dify DSL:

```sh
python -m nexus.services.product_agent_registry.native_build \
  --source source.yml --executable /trusted/scode \
  --runtime-commit FULL_40_CHARACTER_COMMIT \
  --runner examples/native-agent/runner.py --output /new/build-context
```

The destination must not exist. The builder checks the original DSL with the
existing Dify compiler, accepts exactly one nonempty system prompt, and records
the source, definition, executable, runner, dependency, and base-image digests.
The supplied commit is a provenance claim to verify against the actual build;
the builder does not attest it. `native-draft.json` deliberately contains no
registered artifact, valid image ID, authorization, grade, or publication.

## Execution boundary

The candidate uses `agent-hosted-stdio/v1`, the existing capability pipe. The
controller must supply the reviewed source/model binding, single-call allowance,
task IAM grant, model gateway, deadline, and cleanup. The worker's loopback
OpenAI endpoint receives only SudoCode requests and translates them to that
pipe. No IAM or Router credential enters the container. Model name, output
ceiling, temperature, roles, and text content are checked; tools, images and
unknown request fields are rejected.

Run the resulting image only under the existing strict container boundary:
no external network, read-only root, non-root user, bounded writable temporary
storage, memory/swap/CPU/PIDs, and a controller-enforced deadline. The runtime
flags alone are not an OS sandbox. The model response's text is transported as
text; it cannot inject native tool-call frames.

SudoCode retains its native environment context alongside the source system
prompt. The source's system prompt and model parameters are preserved; the
whole prompt is not byte-identical to the original platform. A same-dataset
comparison is therefore required before approving a conversion.

Automated builder/bridge tests exercise rejection, provenance, exact request
translation and a loopback HTTP/stdio roundtrip. Their fake executable is not
native-runtime acceptance. A real compiled SudoCode run, exact image isolation,
review, and Work execution need separate evidence.

## Register a conversion candidate

The owner API `POST /api/v2/product-agents/native/conversions` records an
immutable `NativeConversion`. Supply `source_artifact_id`, a separately
registered native target `asset_id` and its `metadata_digest`, the actual
`NativeImage`, `request_id` and `initiated_by`. This management API requires an
authenticated organization administrator. Generic hosted-artifact import
rejects a native image.

The source must have current, independently approved L2 evidence for that exact
artifact and metadata. The owner recompiles the target definition from the
stored Dify plan and validates its model, output bound, runtime lock and
definition digest. The target declares internal hosting, requested L3, literal
text, no data/tool connections and the same model resource. Core-domain source
assets cannot be converted into an office-domain target.

Registration retains the source acceptance snapshot and its issuer/digest,
source artifact/spec/metadata, target metadata, native image/runtime and exact
spec bytes. It leaves the original L2 asset and publication unchanged. It does
not approve the target: the result explicitly says `registered_only`, and L3
admission remains blocked pending native evaluation and six-family execution
evidence. A runtime commit or image label submitted by an administrator is not
independent build attestation.

`GET /native/conversions` and `GET /native/conversions/{id}` are read-only and
organization scoped. Schema initialization uses the existing explicit operator
command; ordinary startup does not migrate legacy data. Exact request replay
returns the immutable result; it never executes the candidate again.

`scripts/qualify_hosted_isolation.py --artifact native-conversion.json ...`
accepts this record and probes the actual native entrypoint under the same
strict sandbox. Its `NativeIsolationEvidence` covers isolation only. It stops
before model dispatch and does not assign L3.

## Native migration evaluation

The existing durable evaluation API accepts `execution_kind: "native"`, with
`artifact_id` identifying the immutable conversion. Both runs use a dataset
registered against the native target's exact metadata and the target's own
single-call IAM grant. For `role: "source_baseline"`, the source runtime profile
is `registry-hosted-artifact-v1` and its `runtime_digest` must equal the
conversion's `source_spec_digest`; this baseline runs the original reviewed L2
image. The candidate runs the native image. Source calls do not inherit old
source credentials or grant the target L3.

As with hosted migration, the baseline must finish before creating a threshold
policy; the candidate binds that policy when it starts. The owner computes the
comparison from all durable cases, preserves unknown outcomes and requires
cleanup. Native success additionally requires exactly one recorded model call.
The source image, native conversion, dataset, target metadata, runtime kind and
policy cannot be substituted across phases. Existing hosted records omit the
new discriminator when serialized, preserving prior evidence digests.

## Native execution profile and admission

`GET /native/conversions/{id}/profile` returns the immutable binding used by
Moss Task resolution and native output verification. The current profile is
`native-independent-text-v1alpha1`:

| Family | Enforced behavior |
| --- | --- |
| Task | Existing `task.sudo.dev/v1alpha1` Task/Attempt, fixed version and independent turns |
| Tool | SudoCode `--no-tools`, deny-all delegation and no tool permission in the task grant |
| Context | Literal synthetic text, no attachments, path expansion or workspace access |
| Memory | SudoCode `--no-memory`; no cross-turn or automatic memory loading |
| Verify | Deterministic output text/one model call/two native trace frames/cleanup checks |
| Event | Canonical Task events and durable native runtime trace |

These are explicit bindings to existing experimental owners and native runtime
controls. They do not activate or claim all stable v1 contract families. A
Verify pass establishes execution shape and cleanup, not business correctness;
the separate same-dataset rubric supplies the bounded comparison result.

An L3 `AcceptanceReport` must contain `native_binding.conversion_id` and the
canonical `profile_digest`, plus the runtime executable/runner/spec digests in
`evidence_refs`. Admission reads the stored comparison and every candidate
case, recomputes verification and rejects incomplete or substituted evidence.
Older completed cases can be verified from immutable facts without rewriting
or rerunning them. The target stays L0 until a separate administrator approves.

The reviewed L2 source, issuer, metadata and acceptance must still be valid;
target acceptance cannot outlive its source. Source suspension, metadata change,
issuer removal or expiry blocks subsequent native admission. Read endpoints do
not publish, promote, migrate or repair records. Moss publishes the original
native spec bytes through the existing Registry and Work uses the shared hosted
Task conversation, with `resolved_runtime: native_sudocode` and visible Verify
evidence. Unknown execution outcomes are never automatically replayed.

Release and deployment qualification remain environment-specific. A fixed
Linux SudoCode binary, verified container capabilities, model/resource mapping,
all current controllers and explicit schema initialization are prerequisites.
An online Router model does not establish offline inference support.
