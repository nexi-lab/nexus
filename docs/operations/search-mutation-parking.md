# Runbook: Search Mutation Parking (#4337)

When the search daemon cannot resolve content for a write event, the
consumer retries the batch (the checkpoint does not advance). Since #4337
retries are **bounded**: permanent failures (file gone) park after
~3 passes (~6 s), transient failures (backend outage) after ~30 passes
(~60 s). A **parked** event is skipped by the live consumer but durably
recorded for re-drive or discard.

## Symptoms

Log lines from `nexus.bricks.search.daemon`:

- Retrying (bounded):
  `<consumer> mutation content unresolved for event_id=... path=... kind=... attempt=N/M — refusing to checkpoint so the consumer retries on next pass`
- Parked (skipped, recorded):
  `Search mutation PARKED for consumer=... event_id=... path=... kind=... after N attempts ...`
- Forced skip:
  `Search mutation checkpoint FORCED for consumer=...: A -> B`

Metrics (Prometheus):

| Metric | Meaning | Suggested alert |
| --- | --- | --- |
| `nexus_search_mutation_parked_total{consumer,kind}` | Events parked | Page on any increase |
| `nexus_search_mutation_parked_current{consumer}` | Currently parked | Warn while > 0 |
| `nexus_search_mutation_unresolved_retries_total{consumer,kind}` | Retry passes (pre-park) | Warn on sustained rate |
| `nexus_search_mutation_parked_evicted_total{consumer}` | Cap evictions (parking storm) | Page on any increase |

## Diagnose

```bash
# Consumer state: parked_count, last_parked, retrying head-of-line blocker
curl -s "$NEXUS_URL/api/v2/search/stats" | jq '.mutation_consumers'

# Full parked entries (admin)
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$NEXUS_URL/api/v2/search/parked" | jq
```

`kind=permanent` → the path's payload is gone (cf. issue #4339 forensics).
`kind=transient` → the content backend was unreachable; content may exist.

## Decide

- **Payload restored / outage over → retry** (re-drives through the
  consumer; success removes the record, failure re-parks with the error):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"consumer": "embedding", "event_ids": null}' \
  "$NEXUS_URL/api/v2/search/parked/retry" | jq
```

(`event_ids: null` retries everything parked for that consumer.)

A response of `{"retried": 0, ...}` means none of the given `event_ids` are
currently parked for that consumer (typo, already retried, or discarded) —
check `GET /api/v2/search/parked` for the live list.

- **Content permanently gone, loss accepted → discard:**

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"consumer": "embedding", "event_ids": ["search:a39822b6-..."]}' \
  "$NEXUS_URL/api/v2/search/parked/discard" | jq
```

- **Consumer wedged on something the gate does not handle → force-advance
  the checkpoint** (skips EVERY event ≤ the new sequence for that
  consumer — use the smallest sequence that unwedges, typically the
  blocker's own `sequence_number` from `/stats`):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sequence": 123456}' \
  "$NEXUS_URL/api/v2/search/consumers/embedding/skip-to" | jq
```

On a daemon whose consumers have never run, the 400 rejection's "current
checkpoint" is the live operation-log maximum (the value consumer startup
would snap to), not 0.

## Notes

- Park records live in the settings store under `search_mutation_parked`
  (file fallback `mutation-parked.json` next to the checkpoints file).
  Capped at 200 entries per consumer; evictions log ERROR and bump
  `..._parked_evicted_total` — an eviction means a parking storm, look for
  a systemic cause (e.g. #4339-scale payload loss) instead of per-event
  triage.
- Restarts reset in-flight retry counters (a poisoned event re-parks within
  seconds); parked records persist.
- A parked event whose content later resolves on its own (e.g. the same
  path is re-written) is auto-unparked by the gate.
