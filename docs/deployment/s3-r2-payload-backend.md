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
