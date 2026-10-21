# Hosted Agent text profile

This experimental profile supplies strict Dify import, immutable artifact records and a
single-run Docker supervisor. Artifact registration does not publish a Version, grant
execution, attest L2 or replace the Product Registry's review/admission rules.

## Supported sources

`dify-0.6.0-linear-text-v1` accepts Dify DSL 0.6.0 advanced-chat with exactly
start → one text LLM → answer. It maps the explicitly reviewed OpenAI-compatible
plugin 0.0.68 digest in `dify_import.py`; it does not load that plugin's code.
The model name, prompts, query template, optional temperature and output limit are
retained. Only `sys.query` substitution is supported. Citations are empty because
no retrieval/context node is supported. Editor positions/titles are non-executable metadata.

The service profile accepts independent text turns. History, conversation variables,
custom inputs, files, knowledge context, vision, tools, code/branch/loop nodes,
retry/error strategies, unknown parameters and unreviewed plugins are rejected.
When the source omits `max_tokens`, this profile explicitly applies a 128-token
output cap, recorded in `HostedImage`; this differs from a platform/model default
and is part of the migration evaluation scope. It is not a full Dify runtime.

`langgraph-stdio-v1` runs a reviewed image containing a real LangGraph project.
The reference build input is one Python module exporting `build_graph(model, trace)`;
the image contains LangGraph 1.0.5 and a complete pinned dependency lock. Larger
projects require a reviewed prebuilt image implementing the same bounded stdio
protocol; arbitrary projects cannot be imported by dropping a directory into the UI.

## Explicit build and import

Prepare a **new** build context with
`python -m nexus.services.product_agent_registry.hosted_build`. Supply `--source`,
`--profile`, `--runtime-files examples/hosted-agent`, `--output`, `--model-resource`
and `--model-name`. Dify model substitution is rejected before files are written.
User Python is copied without being executed by the management process.

The reference base is pinned to a Python 3.12 image digest. `requirements.lock`
contains the exact Linux x86_64 / CPython 3.12 wheels and SHA-256 hashes used by
the tested image. Preload those wheels in `wheelhouse/` and the base image; build
with `docker build --network none --pull=false`. Installation uses `--no-index`
and `--require-hashes`. Other platforms need their own verified wheel closure.

`descriptor-draft.json` deliberately omits an image ID. After building, record the
actual `docker image inspect ... --format '{{.Id}}'` digest as `image_id`. The
image labels bind protocol, source and dependency-lock digests. A tag, all-zero
placeholder or a mismatching label cannot qualify as the runtime image.

The existing explicit Registry `--initialize-schema` command adds the hosted table.
Normal startup and reads never create tables or migrate existing data. Authenticated
management APIs beneath `/api/v2/product-agents` are:

- `POST /hosted/dify/inspect`: raw DSL, at most 256,000 bytes; validation only.
- `POST /hosted/artifacts`: `RegisterHostedArtifact`; imports an immutable descriptor
  and compiled Dify plan with organization, asset metadata and actor provenance.
- `GET /hosted/artifacts?asset_id=...&after=...`: ten records per page.
- `GET /hosted/artifacts/{id}`: an immutable management record.

The gateway must enforce human owner/admin authorization before using its service
credential. Owner APIs enforce actual authenticated admin/zone, current asset
metadata, internal hosting, platform and model-resource matching. Exact retries
return the same record; changed payloads with the same request ID conflict. Imports
do not start containers and do not alter asset grade or publication state.

## Runtime and trust boundaries

`HostedDockerRunner` reuses `SandboxSecurityProfile.strict()`: no network, read-only
root, no host volumes/devices, no added capabilities, non-root UID 65532, Docker's
default seccomp, no-new-privileges and bounded memory/CPU/PIDs/tmpfs. Images must
already exist; runtime never pulls or builds. The Docker binary/connection is an
operator choice, never request data. Docker daemon access belongs to the trusted
supervisor, never to the agent container.

All model effects leave through `agent-hosted-stdio/v1`. The host chooses the model
resource and checks IAM before each actual dispatch. Workers cannot supply model
URLs, credentials or attribution. Duplicate/out-of-order/oversized frames,
unrecognized tool requests and calls beyond the granted cap are rejected. Traces
are explicitly marked **worker_reported**; only gateway receipts prove model effects.

A completed frame plus clean exit is required. Timeout/cancel does not imply an
already dispatched model call was cancelled. Container cleanup is explicit and
verified; if cleanup cannot be established the result is `HOSTED_CLEANUP_UNCONFIRMED`.
There is no automatic retry. Durable task admission, grant revocation, recovery
after supervisor loss and trusted model metering belong to the enclosing service.

## Evidence and remaining integration

On 2026-10-21, a real Dify 1.14.2 application supplied the pre-migration baseline.
Its exported DSL and an actual LangGraph application each ran six synthetic
arithmetic inputs using `gemini-3-flash-preview` through real Nexus IAM grants and
an actual new-api Router. The baseline and both hosted samples scored 6/6 under
the same exact-integer rubric. Production supervisor modules and offline-built,
fixed-image artifacts also repeated both hosted samples at 6/6. The early container
probe verified network/root-write denial and non-root UID.

This evidence covers those samples only. It is not complete R2/R3 acceptance:
the LangGraph pre-migration baseline, full artifact-to-task/UI flow, versioned
evaluation records, production content-gated model path, acceptance attestation,
restart/HA and offline model service qualification still need integration and
verification. Pure protocol tests and synthetic inputs do not establish L2 by themselves.
