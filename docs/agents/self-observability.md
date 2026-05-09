# Agent Self-Observability

Every Nexus agent has access to a JSONL log of its own activity at:

```
/.activity/{utc_date}/{agent_id}.jsonl
```

Use the same `cat`, `grep`, and `jq` you already use to inspect any other
mount.

## Schema

```json
{"ts":"2026-05-09T23:42:11.043Z","kind":"op","op":"read","path":"/s3/bucket/foo.txt","bytes":12834,"ms":43}
{"ts":"2026-05-09T23:42:12.110Z","kind":"exec","cmd":"grep needle /gh/owner/repo/README.md","exit_code":0,"ms":215}
{"ts":"2026-05-09T23:42:14.221Z","kind":"op","op":"write","path":"/local/notes.md","bytes":412,"ms":8}
```

`ts` is ISO-8601 UTC with millisecond precision and a `Z` suffix. `cmd` is
truncated to 4 KB; truncated records carry `"cmd_truncated": true`.

## Examples

What did I read in the last hour?

```bash
grep '"kind":"op"' /.activity/2026-05-09/me.jsonl | grep '"op":"read"' | tail
```

How much time did I spend on Slack today?

```bash
jq 'select(.path | startswith("/slack/")) | .ms' /.activity/2026-05-09/me.jsonl \
  | awk '{s+=$1} END {print s}'
```

What was my last failed command?

```bash
grep '"kind":"exec"' /.activity/2026-05-09/me.jsonl \
  | jq 'select(.exit_code != 0)' | tail -1
```

Replace `me.jsonl` with your own `agent_id`.

## Isolation

- Each agent can read only its own log file. ReBAC denies cross-agent reads.
- The mount is read-only for agents.
- Operators with `is_admin` can read any agent's log.

## Storage and retention

- Backed by RAM. Default cap **10 MB per agent per day**, configurable via
  `NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES`.
- When the cap is hit, oldest lines are evicted (ring buffer). The most
  recent activity is always available.
- Retention defaults to **7 days** in RAM, configurable via
  `NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS`. No disk archive in v1.
- `NEXUS_ACTIVITY_AGENT_LOG_ENABLED=0` disables the feature entirely.
- `NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES` (default 4096) controls cmd
  truncation length.

## Metrics

- `nexus_activity_agent_log_lines_dropped_total{reason}` —
  reason ∈ {`ring_evict`, `recursion`, `no_agent`}.
- `nexus_activity_agent_log_bytes{agent_id}` — current per-agent buffer size.

## Limitations (v1)

- No streaming reads (`tail -f`) — re-read for new lines.
- System ops with no agent actor are not recorded.
- No on-disk archive.
- Mount + ReBAC runtime wiring is in progress; the JSONL records are
  generated and stored, but exposing them through the FS namespace
  requires the agent_log brick to be wired into mount_service + rebac
  (tracked separately).
