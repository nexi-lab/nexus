# Connector Package Isolation Pilot - Design

**Issue:** [#3964](https://github.com/nexi-lab/nexus/issues/3964) - refactor: isolate optional connector dependencies behind installable connector packages
**Date:** 2026-05-05
**Pilot connectors:** S3 (`path_s3`) and Slack (`slack_connector`)

## Context

Issue #3830 delivered the shared connector catalog foundation: built-in connector entries live in `src/nexus/backends/_manifest.py`, optional implementation modules are imported lazily, typed runtime dependencies are checked at mount time, and slim/full installs share the same runtime registry. Issue #3962 added the extension metadata layer so connector metadata can eventually be listed without importing implementation modules.

The remaining #3964 problem is package-boundary isolation. Optional connector code still ships inside the same Python namespace and wheel surface, and most built-in connectors are only represented in the extension store through the partial legacy adapter. That means connector discovery can list names and runtime deps, but it does not yet expose complete install metadata, connection args, or capabilities for most connector packages without binding to the legacy runtime inventory.

## Goal

Define the installable connector package layout and migrate one storage connector plus one API/OAuth connector as a low-risk pilot. The pilot must preserve the unified connector registry while proving that connector metadata is discoverable without importing optional SDK modules.

## Non-Goals

- Moving every connector into separate distributions in one PR.
- Removing `CONNECTOR_MANIFEST` as the runtime mount catalog.
- Replacing `BackendFactory.create()` or the existing `@register_connector` runtime binding path.
- Redesigning OAuth authorization UX.
- Making Slack or S3 work without their declared runtime dependencies.

## Decisions

| Decision | Choice |
|---|---|
| Pilot pair | S3 (`path_s3`) and Slack (`slack_connector`) |
| First package boundary | Manifest-first package bundle contract, not immediate file movement |
| Runtime registry | Keep `CONNECTOR_MANIFEST` authoritative for mount-time lookup during the pilot |
| Metadata discovery | Add metadata-complete `_manifest.py` files for migrated connectors |
| Install hints | Declare hints in extension `RuntimeDep` records and keep mount-time hints in legacy runtime deps |
| CI strategy | Add focused no-optional-deps import/discovery tests first; add per-package wheel jobs once packages physically split |

## Package Layout

The target connector bundle layout is:

```text
packages/nexus-connector-s3/
  pyproject.toml
  src/nexus_connector_s3/
    _manifest.py
    runtime.py

packages/nexus-connector-slack/
  pyproject.toml
  src/nexus_connector_slack/
    _manifest.py
    runtime.py
```

Each package exposes a manifest entry point:

```toml
[project.entry-points."nexus.connectors"]
s3 = "nexus_connector_s3._manifest:MANIFEST"
slack = "nexus_connector_slack._manifest:MANIFEST"
```

The manifest module imports only `nexus.extensions.manifest`, `nexus.extensions.types`, and standard-library modules. It must not import `boto3`, `slack_sdk`, or the connector implementation module. The runtime module owns the connector class import/registration when the connector is actually mounted.

During the pilot PR, in-tree connectors keep their current file locations. The in-tree `_manifest.py` files use the same shape as the future package manifests so they can be moved into connector packages later without changing the metadata contract.

## Metadata Model

Add metadata-complete connector manifests for:

- `src/nexus/backends/connectors/s3/_manifest.py`, a metadata-only connector package that points at the existing `nexus.backends.storage.path_s3` implementation. This location matches the current extension index scanner, which walks `src/nexus/backends/connectors/*/_manifest.py`.
- `src/nexus/backends/connectors/slack/_manifest.py`.

The S3 manifest declares:

- `name="path_s3"`
- `module="nexus.backends.storage.path_s3"`
- `factory="PathS3Backend"`
- `service_name="s3"`
- `runtime_deps=(RuntimeDep(kind="python", name="boto3", extras=("s3",), install_hint="pip install nexus-fs[s3]"),)`
- `import_probes=("boto3",)`
- connection args matching `PathS3Backend.CONNECTION_ARGS`
- capabilities `{"rename", "directory_listing", "path_delete", "streaming", "batch_content", "signed_url", "multipart_upload", "native_versioning", "resumable_upload"}`

The Slack manifest declares:

- `name="slack_connector"`
- `module="nexus.backends.connectors.slack.connector"`
- `factory="PathSlackBackend"`
- `service_name="slack"`
- `runtime_deps=(RuntimeDep(kind="python", name="slack-sdk", extras=("slack",), install_hint="pip install nexus-fs[slack]"), RuntimeDep(kind="service", name="token_manager"))`
- `import_probes=("slack_sdk",)`
- connection args for the public mount configuration fields accepted by `PathSlackBackend` (`token_manager_db`, `user_email`, `provider`, and `max_messages_per_channel`). Slack does not currently declare a `CONNECTION_ARGS` class attribute, so the metadata manifest becomes the discovery source for these fields.
- `user_scoped=True`
- capabilities `{"user_scoped", "token_manager", "oauth", "readme_doc"}`

The extension store should prefer these metadata-complete manifests over the partial legacy adapter entries. Existing tests already assert that migrated records report `metadata_complete=True`; this pilot extends that coverage to S3 and Slack.

## Runtime Flow

Runtime mounting remains unchanged:

1. `BackendFactory.create("path_s3", config)` calls `_ensure_optional_backends_registered()`.
2. `_register_optional_backends()` registers placeholders from `CONNECTOR_MANIFEST`.
3. Runtime deps are checked through `check_runtime_deps(info.runtime_deps)`.
4. Missing dependencies raise `MissingDependencyError` with install hints before connector construction.
5. If deps are present, the implementation module imports and binds the connector class to the placeholder.

This keeps #3830 behavior stable while #3964 moves metadata and package ownership forward.

## Error Handling

Discovery-time errors remain isolated:

- Broken `_manifest.py` files are skipped in non-strict runtime loading and fail loudly in strict index verification.
- Optional SDK absence must not affect `import nexus`, `import nexus.backends`, or extension listing.
- Mounting `path_s3` without `boto3` must mention `pip install nexus-fs[s3]` when the active install supports `nexus-fs` extras, or the raw package fallback otherwise.
- Mounting `slack_connector` without `slack_sdk` or `token_manager` must enumerate all missing requirements.

## Tests

Add unit tests around the pilot manifests:

- `tests/extensions/test_store.py`: `path_s3` and `slack_connector` return `metadata_complete=True` from `get_store()` and retain their service names.
- `tests/extensions/test_store.py`: metadata discovery does not import `boto3`, `slack_sdk`, or the S3/Slack implementation modules.
- `tests/extensions/test_index.py`: duplicate or malformed connector manifests still fail strict index generation.
- `tests/unit/backends/test_runtime_deps.py` or a focused factory test: missing S3 and Slack deps produce actionable install hints through the existing mount-time checker.

Add slim import/discovery coverage:

- In a base slim wheel with no connector extras, import `nexus`, `nexus.backends`, and `nexus.extensions.store`.
- List connector manifests and verify `path_s3` and `slack_connector` metadata is present.
- Assert the optional SDK modules are not imported as a side effect.

## CI

Extend `.github/workflows/slim-wheel-smoke.yml` first because it already builds a base slim wheel and installs it without connector extras. Add a focused test or test parameter that verifies core imports and connector metadata discovery with no optional connector deps installed.

Once physical connector packages exist, add a separate connector-package matrix:

| Package | Install command | Smoke |
|---|---|---|
| `nexus-connector-s3` | `pip install ./packages/nexus-connector-s3` | manifest discovery and missing-credential mount check |
| `nexus-connector-slack` | `pip install ./packages/nexus-connector-slack` | manifest discovery and missing-token mount check |

## Rollout

1. Land the manifest-first S3 and Slack pilot in-tree.
2. Add CI proving no-optional-deps import and metadata discovery.
3. Split the pilot manifests and runtimes into `packages/nexus-connector-s3` and `packages/nexus-connector-slack`.
4. Convert remaining target connectors after the pilot package contract is stable: GCS, Google Workspace/Drive/Gmail/Calendar, and GitHub.
5. Retire legacy adapter coverage only after every built-in connector has a metadata-complete manifest.

## Acceptance Criteria Mapping

| #3964 criterion | Pilot coverage |
|---|---|
| Define connector package/bundle layout | This design defines package directories, manifest entry points, and runtime module boundaries |
| Add install hints to connector metadata | S3 and Slack extension manifests carry explicit runtime dep install hints |
| Add mount-time dependency checks | Existing #3830 checker remains the enforcement point; tests pin S3/Slack behavior |
| Convert one storage and one API/OAuth connector | S3 and Slack are the pilot migrations |
| Add CI for core import with no optional deps | Slim-wheel smoke extends base no-extras coverage |
| Add CI for migrated packages/bundles | Deferred until physical packages are created after the in-tree pilot |

## Open Follow-Up

The one deliberate deferral is the physical package split. The pilot first proves the metadata and CI contract in-tree. Moving files into separate distributions should be a follow-up PR because it changes wheel ownership, package data, and release automation at the same time.
