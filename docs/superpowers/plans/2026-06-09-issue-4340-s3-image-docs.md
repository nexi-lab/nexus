# Issue #4340 — S3/R2 Deployment Guide + `:edge` Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document the S3/R2 payload-backend configuration for production deployments, prove the published `ghcr.io/nexi-lab/nexus:edge` image serves a `path_s3` mount end to end, and update issue #4340 with evidence plus a release-cut ask.

**Architecture:** Docs-only change to the monorepo (one new guide + one mkdocs nav line + the committed spec). Validation runs against the *published* image (never `--build`) with a MinIO sidecar; its transcript feeds the issue comment. The publish half of #4340 is already merged (#4323 / nexus-vfs#27) — this plan adds zero Rust/CI/Dockerfile changes.

**Tech Stack:** Markdown/mkdocs, `gh` CLI, Docker + MinIO (`minio/minio`, `minio/mc`), nexus stack CLI (`nexus up` — use the `nexus-stack` skill at execution time), REST API v2.

**Verified facts the content below relies on** (re-verified 2026-06-09 against develop `17d29a908`):
- Connector registers as `@register_connector("path_s3")` ([path_s3.py:33](../../../src/nexus/backends/storage/path_s3.py)); factory instantiates `connector_cls(**config)` — config keys are the constructor params: `bucket_name` (required — NOT `bucket`), `region_name`, `prefix`, `access_key_id`, `secret_access_key`, `session_token`, `endpoint_url`, `signature_version` (default `s3v4`), `credentials_path`.
- Mount API: `POST /api/v2/connectors/mount` body `{connector_type, mount_point, config}` ([connectors.py:75-80,597](../../../src/nexus/server/api/v2/routers/connectors.py)).
- File APIs: `POST /api/v2/files/write` `{path, content, encoding?}`, `GET /api/v2/files/read?path=…`, `GET /api/v2/files/read-url` (signed-URL path from #4323).
- mkdocs nav deployment section: [mkdocs.yml:211-212](../../../mkdocs.yml).
- Last release v0.10.1 (2026-05-29) predates #4323 → `:latest`/`:stable` lack `nexusd-full`; `:edge` has it (develop publishes green since 2026-06-08).

---

### Task 1: Deployment guide + nav entry

**Files:**
- Create: `docs/deployment/s3-r2-payload-backend.md`
- Modify: `mkdocs.yml:212` (add one nav line after `MCP Hub Mode`)

- [ ] **Step 1: Create the guide with this exact content**

````markdown
# S3 / R2 Payload Backend

Store file payloads in S3-compatible object storage (AWS S3, Cloudflare R2, MinIO)
instead of — or alongside — the local data volume. Motivation: a single data volume
as the sole copy of payloads is a correlated-loss risk (#4339) and contends with
other on-volume writers such as `activity.db` (#4336). An object-store mount makes
payload durability the bucket's problem.

## Image requirements

The S3 driver lives in the Rust kernel binary shipped inside the server image. The
image must contain `nexusd-full` (= `nexusd-cluster` + the `driver-s3` backend,
introduced in nexus-vfs#27 and wired into the image in #4323):

- `ghcr.io/nexi-lab/nexus:edge` — S3-capable since 2026-06-08.
- `ghcr.io/nexi-lab/nexus:latest` / `:stable` / tags ≤ `v0.10.1` — **not** S3-capable;
  the first release cut after 2026-06-08 will be.

Self-check an image before deploying:

```bash
docker run --rm --entrypoint nexusd-full ghcr.io/nexi-lab/nexus:edge --version
```

If the entrypoint is missing, the image predates #4323 — use `:edge` or a newer
release. (`nexus-stack.yml` defaults to `:latest`; override with
`NEXUS_IMAGE_REF=ghcr.io/nexi-lab/nexus:edge` until the next release.)

## Mounting an S3/R2 bucket

Mount via the REST API (host CLI not required):

```bash
curl -sS -X POST "$NEXUS_URL/api/v2/connectors/mount" \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "connector_type": "path_s3",
    "mount_point": "/mnt/r2",
    "config": {
      "bucket_name": "my-payloads",
      "prefix": "nexus",
      "endpoint_url": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com",
      "region_name": "auto",
      "access_key_id": "<KEY_ID>",
      "secret_access_key": "<SECRET>"
    }
  }'
```

Config keys (constructor parameters of the `path_s3` connector — note it is
`bucket_name`, not `bucket`):

| Key | Required | Notes |
|---|---|---|
| `bucket_name` | yes | Bucket must already exist; verified at mount time. |
| `endpoint_url` | for R2/MinIO | Omit for AWS S3. |
| `region_name` | recommended | The transport falls back to `us-east-1` with a warning — set it explicitly. R2 uses `auto`. |
| `access_key_id` / `secret_access_key` | no | Omit to use the AWS default credential chain (env vars, shared credentials file, IAM role). |
| `prefix` | no | Key prefix inside the bucket. |
| `session_token` | no | For temporary STS credentials. |
| `signature_version` | no | Default `s3v4` — correct for R2 and MinIO. |

After mounting, normal file APIs route through the bucket:

```bash
curl -sS -X POST "$NEXUS_URL/api/v2/files/write" \
  -H "Authorization: Bearer $NEXUS_API_KEY" -H "Content-Type: application/json" \
  -d '{"path": "/mnt/r2/hello.txt", "content": "payload lives in the bucket"}'

curl -sS "$NEXUS_URL/api/v2/files/read?path=/mnt/r2/hello.txt" \
  -H "Authorization: Bearer $NEXUS_API_KEY"
```

## Cloudflare R2

1. Create an R2 bucket and an **R2 API token** (Object Read & Write); Cloudflare
   gives you an access-key/secret pair for the S3 API.
2. `endpoint_url`: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
3. `region_name`: `auto` (R2's documented region).
4. Reads can additionally use the signed-URL fast path
   (`GET /api/v2/files/read-url`, #4323) — nexus validates permissions and returns a
   short-TTL presigned URL so bytes stream from R2/CDN, not through nexus. This path
   currently requires inline mount credentials (not the env credential chain).

## MinIO (local / self-hosted)

```bash
docker run -d --name minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data
```

Mount config: `endpoint_url: "http://minio:9000"` (container-network name, or
`http://localhost:9000` from the host), `region_name: "us-east-1"`, credentials as
above. If nexus runs in the stack network, attach MinIO to it:
`docker network connect <nexus-network> minio`.

## Credentials options

- **Inline mount config** (`access_key_id`/`secret_access_key`): simplest; required
  today for the signed-URL read path.
- **AWS default chain**: omit the keys; the backend resolves env vars
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`), shared credentials file, or IAM
  role. `nexus-stack.yml` already forwards `AWS_*` env vars and `~/.aws/` into the
  server container.

## Troubleshooting

- **`unknown backend_type: 's3'`** — the kernel binary in your image has no S3
  driver: the image predates #4323 (any `:latest` ≤ v0.10.1). Switch to `:edge` or
  the first newer release and re-run the self-check above.
- **Mount fails with bucket verification error** — bucket missing, wrong
  `endpoint_url`, or credentials lack read/write on the bucket.
- **Region warning in kernel logs** — set `region_name` explicitly (`auto` for R2).
````

- [ ] **Step 2: Add the mkdocs nav entry**

In `mkdocs.yml`, directly after the line `    - MCP Hub Mode: deployment/mcp-hub-mode.md` (line 212), insert:

```yaml
    - S3 / R2 Payload Backend: deployment/s3-r2-payload-backend.md
```

- [ ] **Step 3: Verify mkdocs accepts the nav (build check)**

Run: `uv run mkdocs build --strict 2>&1 | tail -5` (from repo root; if `mkdocs` is not in the default group, `uv run --group docs mkdocs build --strict`; if the strict build fails on pre-existing unrelated warnings, fall back to confirming our page renders: `uv run mkdocs build 2>&1 | grep -i 's3-r2\|error'`)
Expected: no error mentioning `s3-r2-payload-backend.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/deployment/s3-r2-payload-backend.md mkdocs.yml
git commit -m "docs(#4340): S3/R2 payload backend deployment guide"
```

---

### Task 2: Validate the published `:edge` image against MinIO

No repo changes. Produces the evidence block for Task 4. **Use the `nexus-stack` skill** for stack lifecycle. Known gotchas: never `--build` here (the point is the *published* image); `nexus env` can report stale ports — trust `docker inspect`; use REST, not the host CLI.

- [ ] **Step 1: Pull image, record digest**

```bash
docker pull ghcr.io/nexi-lab/nexus:edge
docker inspect ghcr.io/nexi-lab/nexus:edge --format '{{index .RepoDigests 0}}'
```
Expected: digest line `ghcr.io/nexi-lab/nexus@sha256:…` — **record it** for Task 4.

- [ ] **Step 2: Binary pre-check**

```bash
docker run --rm --entrypoint nexusd-full ghcr.io/nexi-lab/nexus:edge --version
docker run --rm --entrypoint /bin/sh ghcr.io/nexi-lab/nexus:edge -c 'ls -l /usr/local/bin/ | grep -E "nexusd-full|nexus-cluster|nexusd-cluster"'
```
Expected: a version string; symlinks `nexus-cluster -> /usr/local/bin/nexusd-full` and `nexusd-cluster -> /usr/local/bin/nexusd-full`. **Record the version string.** If `nexusd-full` is missing → STOP, this is a finding (publish broken); skip to Task 4 and report it instead of success.

- [ ] **Step 3: Start the stack from the published image**

```bash
NEXUS_IMAGE_REF=ghcr.io/nexi-lab/nexus:edge nexus up
docker ps --filter ancestor=ghcr.io/nexi-lab/nexus:edge --format '{{.Names}} {{.Image}} {{.Ports}}'
```
Expected: server container running with image `ghcr.io/nexi-lab/nexus:edge`. Derive `NEXUS_URL` from the published port (`docker inspect <ctr> --format '{{json .NetworkSettings.Ports}}'`) and `NEXUS_API_KEY` from `nexus env` output (fallback: `docker exec <ctr> env | grep -i key`). Sanity: `curl -sS $NEXUS_URL/health` (or `/api/v2/health`) returns OK.

- [ ] **Step 4: MinIO sidecar + bucket on the stack network**

```bash
NET=$(docker network ls --format '{{.Name}}' | grep nexus | head -1)
docker run -d --name minio-4340 --network "$NET" \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data
docker run --rm --network "$NET" --entrypoint /bin/sh minio/mc -c \
  'mc alias set m http://minio-4340:9000 minioadmin minioadmin && mc mb m/nexus-payloads'
```
Expected: `Bucket created successfully 'm/nexus-payloads'`.

- [ ] **Step 5: Mount `path_s3` via REST**

```bash
curl -sS -X POST "$NEXUS_URL/api/v2/connectors/mount" \
  -H "Authorization: Bearer $NEXUS_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "connector_type": "path_s3",
    "mount_point": "/mnt/s3",
    "config": {
      "bucket_name": "nexus-payloads",
      "endpoint_url": "http://minio-4340:9000",
      "region_name": "us-east-1",
      "access_key_id": "minioadmin",
      "secret_access_key": "minioadmin"
    }
  }'
```
Expected: 200 with mount info. `unknown backend_type: 's3'` here = the headline failure → STOP, record logs (`docker logs <ctr> | tail -50`), go to Task 4 as a finding.

- [ ] **Step 6: Write → read → verify bytes**

```bash
curl -sS -X POST "$NEXUS_URL/api/v2/files/write" \
  -H "Authorization: Bearer $NEXUS_API_KEY" -H "Content-Type: application/json" \
  -d '{"path": "/mnt/s3/issue-4340-proof.txt", "content": "edge image serves path_s3 — issue #4340"}'
curl -sS "$NEXUS_URL/api/v2/files/read?path=/mnt/s3/issue-4340-proof.txt" \
  -H "Authorization: Bearer $NEXUS_API_KEY"
```
Expected: write returns `content_id`/`version`/`size`; read returns the exact string `edge image serves path_s3 — issue #4340`.

- [ ] **Step 7: Confirm the object physically in the bucket**

```bash
docker run --rm --network "$NET" --entrypoint /bin/sh minio/mc -c \
  'mc alias set m http://minio-4340:9000 minioadmin minioadmin && mc ls --recursive m/nexus-payloads'
```
Expected: at least one object listed (path shape may include internal prefixes). **Record the listing line.**

- [ ] **Step 8: Teardown + transcript**

```bash
nexus down
docker rm -f minio-4340
```
Collect for Task 4: image digest (Step 1), `nexusd-full --version` output (Step 2), mount 200 response (Step 5), byte-identical read (Step 6), `mc ls` line (Step 7).

---

### Task 3: Push branch + open the docs PR

**Files:** none (git/gh only). Branch `docs-4340-s3-r2-payload-backend` already holds the spec commit + Task 1 commit.

- [ ] **Step 1: Confirm linear history & push**

```bash
git log --oneline origin/develop..HEAD   # expect exactly: spec commit + guide commit, no merges
git push -u origin docs-4340-s3-r2-payload-backend
```
(If develop moved meanwhile: `git rebase origin/develop` — never merge; repo blocks merge commits.)

- [ ] **Step 2: Open PR**

```bash
gh pr create --repo nexi-lab/nexus --base develop \
  --title "docs(#4340): S3/R2 payload backend deployment guide" \
  --body "$(cat <<'EOF'
## Summary
Deployment guide for the S3/R2 payload backend (closes the documentation half of #4340).

The publish half is already done: nexus-vfs#27 added `nexusd-full` (cluster + `driver-s3`) and #4323 ships it in the main image — `:edge` is S3-capable since 2026-06-08. This PR documents image requirements, the `path_s3` mount config (`bucket_name`, `endpoint_url`, `region_name`, credentials), R2 and MinIO specifics, and the `unknown backend_type: 's3'` troubleshooting path. Adds the page to the mkdocs nav.

Validated against the published `:edge` image + MinIO end to end (mount → write → byte-identical read → object physically in bucket); transcript in the issue comment on #4340.

## Notes
- Docs-only; no Rust/CI/Dockerfile changes.
- `:latest`/`:stable` (v0.10.1) still predate #4323 — flagged in #4340 as a release-cut ask.
EOF
)"
```
Expected: PR URL printed. **Record it** for Task 4. (If validation produced a failure finding instead, hold the PR — see Task 2 stops — and adjust the comment in Task 4.)

- [ ] **Step 3: Watch CI**

```bash
gh pr checks --repo nexi-lab/nexus --watch
```
Expected: green (docs-only). Benchmark/size gates shouldn't trigger; if something docs-unrelated flakes, note it, don't chase.

---

### Task 4: Issue #4340 comment

- [ ] **Step 1: Post the comment** (fill the three ⟨measured⟩ slots from Task 2/3 records; if Task 2 failed, replace the Validation section with the failure logs and swap the recommendation to "keep open — publish broken")

```bash
gh issue comment 4340 --repo nexi-lab/nexus --body "$(cat <<'EOF'
## Status: publish half done — main-image route; docs PR up; one gap left (`:latest`)

**Published image.** This resolved via the main image rather than an `-s3` variant: nexus-vfs#27 (merged 2026-06-08) added the `nexusd-full` profile binary (= `nexusd-cluster` + `backends/driver-s3`, keeping the cluster binary pure per the kernel-team scope ruling), and #4323 (merged 2026-06-08) installs it in the image, symlinked as `nexus-cluster`/`nexusd-cluster`. The cluster-binary size gate is untouched — it gates the standalone `nexusd-cluster` artifact, not the image. `docker-publish.yml` runs on develop are green since the merge → **`ghcr.io/nexi-lab/nexus:edge` is S3-capable**.

**Validation against the published image** (⟨digest⟩, `nexusd-full --version` → ⟨version⟩):
- `path_s3` mount via `POST /api/v2/connectors/mount` (MinIO endpoint) → 200
- REST write → byte-identical read through the mount
- object physically present in the bucket: ⟨mc ls line⟩

**Remaining gap — `:latest`.** Last release is v0.10.1 (2026-05-29), which predates #4323; `nexus-stack.yml` defaults to `:latest`, so default-tag deployments still hit `unknown backend_type: 's3'`. Maintainers: please cut the next release so the default tag picks up `nexusd-full`. Until then: `NEXUS_IMAGE_REF=ghcr.io/nexi-lab/nexus:edge`.

**Docs** (`s3_endpoint` + R2/MinIO credentials — ask 2 of this issue): ⟨docs PR link⟩.

With the docs PR merged I'd consider this closeable — `:latest` is a release-cadence matter rather than missing code.
EOF
)"
```
Expected: comment URL printed.

- [ ] **Step 2: Hand close decision back**

Do NOT close the issue. Report comment + PR links to the user (issue author) — close call is theirs.
