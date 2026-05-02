# Zone Archives

`nexus archive` produces signed, credential-stripped snapshots of one or more
zones. Use it for backup, disaster recovery, host migration, contractor zone
hand-off, or compliance audit takeout.

## Quick start

```bash
# Create an archive of the eng zone
nexus archive create --zone eng --output eng-2026-05-01.nexus

# Verify a downloaded archive
nexus archive verify eng-2026-05-01.nexus

# Restore on a fresh nexus, re-injecting credentials
nexus archive restore eng-2026-05-01.nexus \
  --inject HUB_TOKEN_eng_hub=$HUB_TOKEN \
  --inject PROVIDER_KEY_anthropic=$ANTHROPIC_KEY
```

## Credential stripping

Every archive runs a two-layer credential stripper before writing:

1. **Schema-aware**: known sensitive columns (provider api_key, federation
   auth_token, webhook secret) are replaced with `${PLACEHOLDER}` strings.
   The manifest lists every placeholder operators must re-inject on restore.
2. **Regex backstop**: free-text fields (doc bodies, settings JSON values)
   are scanned for `sk-ant-…`, `ghp_…`, `AKIA…`, `xoxb-…`, etc. Matches are
   redacted with `***REDACTED***` and a warning logged.

Restore aborts before any write if any placeholder is still un-injected.

## Signing

Each archive is signed with an ed25519 keypair stored at
`~/.nexus/archive_signing_key`. The pubkey is embedded in `signatures.json`.

```bash
# Rotate the signing key (old archives still verify)
nexus archive keys rotate

# Pin a known signer in the trust store (TOFU)
nexus archive keys trust <pubkey-b64> --label "alice@hub"

# Restore that requires a pre-trusted signer
nexus archive restore eng.nexus --require-trusted --inject ...
```

## Scheduled archives

Hub-only. Add to `nexus.yaml`:

```yaml
archive:
  schedule: "0 2 * * *"
  retention:
    daily: 7
    weekly: 4
    monthly: 6
  destination:
    kind: s3
    bucket: my-nexus-archives
    prefix: prod/
    region: us-east-1
```

The scheduler runs every minute, checks the cron expression, and on match
creates archives for all zones, uploads to the destination, and runs GFS
retention against the destination listing.

## Audit export

For compliance takeout:

```bash
nexus archive create --zone eng --audit \
  --from 2026-04-01 --to 2026-05-01 \
  --output eng-april.nexus
```

Includes only documents with create/modify timestamps in the window plus the
slice of activity events from the same window. Full policy snapshot at the
window end is included regardless of window for auditor context.
