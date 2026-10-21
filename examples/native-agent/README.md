# Independent-text native build candidate

This builder converts the supported Dify source definition into a SudoCode
runtime configuration. The target does not load Dify or LangGraph. It runs the
native SudoCode engine with tools and automatic memory disabled, accepting
literal text. Unsupported source roles, nodes, tools, memory, and files are
rejected; they are never silently flattened into a prompt.

This is build preparation, **not L3 qualification or publication**. The Registry
continues to reject L3 until native artifacts, six-contract execution evidence,
independent evaluation, and explicit approval are implemented and verified.

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
