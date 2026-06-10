# Issue #4340 — S3-capable image: deployment docs, validation, and issue closure

- **Issue**: [nexi-lab/nexus#4340](https://github.com/nexi-lab/nexus/issues/4340) — "Publish an S3-capable image (driver-s3) for production use - R2/MinIO payload backends"
- **Date**: 2026-06-09
- **Status**: design approved (scope + approaches confirmed by issue author)

## Context: the publish half is already done

The issue asks for two things: (1) a published image whose cluster binary can serve
`path_s3` mounts, and (2) documentation for `s3_endpoint` + credentials config for
R2/MinIO. Item (1) resolved while the issue was being filed:

- nexus-vfs [#27](https://github.com/nexi-lab/nexus-vfs/pull/27) (merged 2026-06-08)
  added the `nexusd-full` profile binary = `nexusd-cluster` + `backends/driver-s3`.
  `nexusd-cluster` itself stays pure (kernel-team scope ruling); the cluster-binary
  size gate (`.github/workflows/cluster-binary-build.yml`) is sidestepped because it
  gates only the standalone `nexusd-cluster` artifact, not the Docker image.
- nexus [#4323](https://github.com/nexi-lab/nexus/pull/4323) (merged 2026-06-08)
  changed the main `Dockerfile` to `cargo install --git nexus-vfs --rev $NEXUS_VFS_REV
  --bin nexusd-full nexus-full`, symlinked as both `nexus-cluster` (what the Python
  factory spawns) and `nexusd-cluster` (back-compat). The build runs
  `nexusd-full --version` as a sanity check.
- `docker-publish.yml` runs on develop are green since the merge, so
  `ghcr.io/nexi-lab/nexus:edge` is S3-capable as of 2026-06-08/09.

**The remaining gap**: `:latest` / `:stable` still point at v0.10.1 (2026-05-29,
pre-#4323), and `nexus-stack.yml` defaults to `:latest` — so production deployments
following defaults still hit `unknown backend_type: 's3'` until the next release tag.
That is a maintainer release-cut decision, not a code change; this work flags it.

## Scope

In scope (this design):

1. **Deployment guide** `docs/deployment/s3-r2-payload-backend.md` (new file).
2. **Validation** that the published `:edge` image actually serves a `path_s3` mount
   end to end (evidence for the issue comment; no repo changes).
3. **Issue update**: comment on #4340 with status + evidence + explicit release-cut
   ask; recommend close (close decision stays with the issue author).

Out of scope:

- Any Rust, Dockerfile, or CI change (done in #4323 / nexus-vfs#27).
- A `-s3` image variant (`:edge-s3` / `:latest-s3`) — moot; driver-s3 ships in the
  main image.
- Cutting the release itself (maintainer prerogative; we ask, not act).
- Full-stack S3 E2E in CI — that is [#4308](https://github.com/nexi-lab/nexus/issues/4308)'s scope.

## Deliverable 1 — deployment guide

**Location**: `docs/deployment/s3-r2-payload-backend.md`, beside `mcp-hub-mode.md` and
`sandbox-profile.md`. Chosen over reviving `docs/archive/s3-connector-backend.md`
(documents the legacy boto3-connector shape; mixing it with the Rust-served path would
confuse) and over a section in `docs/architecture/backend-architecture.md` (wrong
altitude for "paste these credentials").

**Content outline**:

1. **What/why** — S3-compatible object storage as the payload backend so the data
   volume is no longer the sole copy of stored payloads (motivation: #4339 volume
   loss, #4336 volume contention).
2. **Image requirements** — needs `nexusd-full` in the image: `:edge` from 2026-06-08
   onward, or the first release tag after v0.10.1. Self-check command:
   `docker run --rm --entrypoint nexusd-full ghcr.io/nexi-lab/nexus:edge --version`.
   One paragraph on what `nexusd-full` is and why plain `nexusd-cluster` lacks S3.
3. **Mounting** — REST `POST /api/v2/connectors/mount` with `connector_type:
   "path_s3"` and the config keys (`bucket`, `prefix`, `endpoint_url`,
   `access_key_id`, `secret_access_key`, `region_name`). The exact key names and
   shapes are verified against `src/nexus/backends/storage/path_s3.py` and the
   connectors router during implementation — not written from memory.
4. **Cloudflare R2 block** — endpoint `https://<account_id>.r2.cloudflarestorage.com`,
   R2 API token → access-key pair. Region value (`auto` is R2's documented value; the
   Rust transport defaults to `us-east-1` and logs a warning on fallback) is verified
   during implementation rather than asserted here; the guide documents whichever is
   confirmed, and recommends setting it explicitly to silence the fallback warning.
5. **MinIO block** — local/dev: run MinIO on the stack network, endpoint
   `http://minio:9000`, example with `minioadmin` credentials.
6. **Credentials options** — inline mount config vs. the AWS env provider chain
   (`nexus-stack.yml` already forwards `AWS_ACCESS_KEY_ID` etc.). Caveat from #4323:
   the signed-URL read path currently requires inline mount credentials.
7. **Troubleshooting** — `unknown backend_type: 's3'` means the image predates #4323
   (any `:latest` ≤ v0.10.1): switch to `:edge` or the next release.

If `docs/index.md` (or another nav surface) lists deployment docs, add a link;
otherwise skip — no new nav invented for one page.

## Deliverable 2 — validation of the published image

Run locally, mirroring the production path; produces the evidence quoted in the issue
comment.

1. `docker pull ghcr.io/nexi-lab/nexus:edge`; record digest.
2. Cheap pre-check: `docker run --rm --entrypoint nexusd-full <image> --version`.
3. `NEXUS_IMAGE_REF=ghcr.io/nexi-lab/nexus:edge nexus up` — **no `--build`** (the
   point is the published image, not local source). Confirm the running container's
   image with `docker inspect` (known gotcha: `nexus env` can report stale ports;
   trust `docker inspect`).
4. MinIO sidecar on the stack network + bucket via `mc`.
5. REST mount: `POST /api/v2/connectors/mount` with `path_s3`, `endpoint_url`
   pointing at the MinIO container.
6. Write a file through the API, read it back byte-identical, and confirm the object
   physically exists in the bucket (`mc ls`).
7. Teardown (`nexus down`, remove MinIO).

**Failure handling**: if any step fails, the failure (with logs) goes into the issue
comment as a finding instead of the docs claiming success, and the docs PR holds until
the cause is understood. The docs must not document a flow that was not observed
working.

## Deliverable 3 — issue comment

Posted on #4340 after the docs PR is open, containing:

- Status: publish half done via the main-image route (merged #4323 + nexus-vfs#27);
  no `-s3` variant needed; size gate untouched.
- Evidence: `:edge` digest + validation transcript summary (mount → write → read →
  object in bucket).
- The `:latest` gap: v0.10.1 predates #4323; explicit ask to maintainers to cut the
  next release so default-tag deployments get S3 support.
- Link to the docs PR.
- Recommendation to close once the docs PR merges.

## PR mechanics

- Branch `docs-4340-s3-r2-payload-backend` off current `origin/develop` (the session
  worktree's original branch carries an unrelated unmerged stack and is left alone).
- Commits: this spec, then the guide. PR targets `develop`; linear history (rebase,
  never merge-commit) per repo rules.
- Docs-only PR: no size gate, no benchmark gating expected.

## Success criteria

1. `docs/deployment/s3-r2-payload-backend.md` exists, with config shapes verified
   against the code, and a working R2 + MinIO example each.
2. Validation transcript shows the published `:edge` image serving a `path_s3` mount
   against MinIO end to end (or a documented failure finding instead).
3. #4340 carries the status comment with evidence and the release-cut ask.
4. Docs PR open against develop, CI green.
