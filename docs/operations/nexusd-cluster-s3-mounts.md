# nexusd-cluster: S3-compatible mounts

`nexusd-cluster` can mount one S3-compatible bucket (AWS S3, Cloudflare
R2, MinIO) at startup, alongside the local host-fs root at `/`. The
mount is declared with environment variables (or the equivalent flags)
and is built by the same `ObjectStoreProvider` the gRPC mount path uses.

> **Build requirement.** The default slim binary does **not** include
> the S3 driver. Build with the `driver-s3` feature:
>
> ```bash
> cargo build -p nexus-cluster --features driver-s3
> ```
>
> If `NEXUS_S3_BUCKET` is set on a binary built without `driver-s3`, the
> daemon **fails fast at startup** with
> `driver 's3' not enabled in current deployment profile`.

## Configuration

| Env | Flag | Required | Default | Notes |
|-----|------|----------|---------|-------|
| `NEXUS_S3_BUCKET` | `--s3-bucket` | declares the mount | — | Set this to enable an S3 mount. |
| `NEXUS_S3_REGION` | `--s3-region` | yes (if bucket) | — | AWS region; Cloudflare R2 uses `auto`. |
| `NEXUS_S3_ACCESS_KEY_ID` | `--s3-access-key-id` | yes (if bucket) | — | Prefer env over flag. |
| `NEXUS_S3_SECRET_ACCESS_KEY` | `--s3-secret-access-key` | yes (if bucket) | — | Prefer env over flag. |
| `NEXUS_S3_ENDPOINT` | `--s3-endpoint` | no | — | Custom endpoint (R2/MinIO). Omit for AWS. |
| `NEXUS_S3_PREFIX` | `--s3-prefix` | no | `` (empty) | Key prefix within the bucket. |
| `NEXUS_S3_MOUNT` | `--s3-mount` | no | `/s3` | Mount point. Must be a non-root path. |

**Security:** pass credentials via environment variables, not flags.
Flag values appear in the process's `argv`, which is world-readable via
`ps`. In Kubernetes, source the keys from a `Secret`; under systemd, use
an `EnvironmentFile` with `0600` permissions.

## Example — AWS S3

```bash
export NEXUS_S3_BUCKET=my-prod-bucket
export NEXUS_S3_REGION=us-east-1
export NEXUS_S3_ACCESS_KEY_ID=AKIA...
export NEXUS_S3_SECRET_ACCESS_KEY=...
export NEXUS_S3_PREFIX=nexus/data        # optional
export NEXUS_S3_MOUNT=/s3                 # optional (default)

nexusd-cluster --bootstrap-mode static
```

Files written under `/s3/...` land in `s3://my-prod-bucket/nexus/data/...`.
The local host-fs root at `/` is unaffected.

## Example — Cloudflare R2

R2 is S3-compatible via a custom endpoint and `region=auto`:

```bash
export NEXUS_S3_BUCKET=my-r2-bucket
export NEXUS_S3_REGION=auto
export NEXUS_S3_ACCESS_KEY_ID=...        # from R2 "Manage R2 API Tokens"
export NEXUS_S3_SECRET_ACCESS_KEY=...
export NEXUS_S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
export NEXUS_S3_MOUNT=/r2

nexusd-cluster --bootstrap-mode static
```

> R2 API tokens must have **Object Read & Write** permission — a
> read-only token passes a bucket check but fails writes with `403
> AccessDenied`.

## Fail-fast behavior

The daemon refuses to start (non-zero exit, message on stderr) when:

- `NEXUS_S3_BUCKET` is set but `NEXUS_S3_REGION`,
  `NEXUS_S3_ACCESS_KEY_ID`, or `NEXUS_S3_SECRET_ACCESS_KEY` is missing or
  empty — the error names the missing variable.
- `NEXUS_S3_MOUNT` is `/` (or only slashes) — `/` is reserved for the
  local host-fs root.
- The binary was built without `--features driver-s3` — the driver gate
  rejects the `s3` driver.

A bad endpoint or unreachable bucket is **not** a startup failure: the
backend is constructed without network I/O, so the first read/write to
the mount surfaces the error instead.

## Scope

- One S3 mount per daemon. Multiple mounts are not yet supported.
- The mount is node-local; it is not replicated across the federation.
  Each node that needs the bucket sets `NEXUS_S3_*` independently.
- GCS startup mounts are not wired (the mechanism is identical; track
  separately).
