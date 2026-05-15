# nexus-fs doctor

Run diagnostic checks on your nexus-fs installation.

## Usage

```bash
nexus-fs doctor
nexus-fs doctor --mount s3://my-bucket
nexus-fs doctor --mount s3://my-bucket --mount gcs://project/bucket
```

## What it checks

`doctor` runs three categories of checks:

### 1. Environment

| Check | What it verifies |
|-------|------------------|
| Python version | Python >= 3.11 |
| nexus-fs version | Package is installed and reports its version |
| Rust accelerator | Optional `nexus-kernel` Rust extension (speeds up hashing) |

### 2. Backends

For each installed backend, doctor checks:

| Check | What it verifies |
|-------|------------------|
| Package installed | `boto3` (S3), `google-cloud-storage` (GCS), etc. |
| Credentials found | AWS credential chain, GCP ADC, OAuth tokens |

If a backend package is not installed, it shows `NOT INSTALLED` with
the install command.

### 3. Mounts (with `--mount`)

When you pass `--mount <uri>`, doctor also checks:

| Check | What it verifies |
|-------|------------------|
| Connectivity | Can reach the bucket/drive and list objects |
| Latency | Round-trip time to the backend |

## Reading the output

```
nexus-fs doctor
┌──────────────┬────────┬──────────────────────────┐
│ Check        │ Status │ Details                  │
├──────────────┼────────┼──────────────────────────┤
│ Python       │ PASS   │ 3.13.2                   │
│ nexus-fs     │ PASS   │ 0.1.0                    │
│ nexus-kernel   │ PASS   │ 0.2.0 (Rust accelerator) │
├──────────────┼────────┼──────────────────────────┤
│ s3 package   │ PASS   │ boto3 installed           │
│ s3 creds     │ PASS   │ from ~/.aws/credentials   │
│ gcs package  │ N/A    │ pip install nexus-fs[gcs] │
│ gdrive       │ N/A    │ pip install nexus-fs[gdrive] │
└──────────────┴────────┴──────────────────────────┘
```

Status values:

| Status | Meaning |
|--------|---------|
| `PASS` | Check succeeded |
| `FAIL` | Check failed — see fix hint |
| `NOT INSTALLED` | Optional package not installed — shows install command |
| `CONNECTED` | Mount connectivity check passed (with latency) |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | One or more checks failed |

## Tips

- Run `doctor` after installing a new backend extra to verify credentials.
- Use `--mount` to test connectivity before writing code.
- If a credential check fails, doctor shows a fix hint (e.g.,
  "run `aws configure`" or "run `gcloud auth application-default login`").
