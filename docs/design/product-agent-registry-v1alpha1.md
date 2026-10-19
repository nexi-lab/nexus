# Product Agent Registry v1alpha1

This experimental product service registers Agent publication metadata. It is separate from
the nexus-vfs PID/lifecycle AgentRegistry. It does not make `agent.sudo.dev/v1` stable and does
not change the support status of the existing ResourceRef or runtime contracts.

## Authority and data

- A Principal is identified by `(trust_domain, agent_name)`. Operator configuration maps each
  trust domain to one authenticated Nexus Zone. A request cannot supply its actor or role.
- Only an authenticated administrator of the mapped Zone may register, publish, decide or
  change a Channel. Zone members may read metadata. Open-access authentication is rejected.
- A Version label is immutable. It binds an existing ResourceRef and SHA-256 digest plus optional
  source evidence references. Every reference must name the same Zone and carry a digest.
- Publication does not approve a Version or promote a Channel. Approval is an explicit,
  append-only decision with a maximum seven-day expiry. Withdrawal is reversible by a new
  approval; revocation is terminal. The latest decision controls resolution.
- Channel promotion/rollback uses compare-and-swap on its revision. Withdrawal, revocation or
  Principal suspension disables affected Channels and increments their revision. Reactivating
  a Principal or renewing approval never silently re-enables a Channel.
- Writers serialize on the Principal row. Commands and their audit/idempotency records commit
  together. SQLite uses explicit transactions; PostgreSQL uses row locks. Reads take a consistent
  transaction and never publish, approve, create schema, or mutate Channels.

## Registration signatures

Versions include Ed25519 signatures over the exact returned UTF-8 `payload_base64` bytes.
The key ID is the SHA-256 of the raw 32-byte public key. Readers verify the allowed key,
signature, payload digest, Principal/Version identity, and equality with the manifest projection.
An unknown or removed key fails closed. Retain old public keys during key rotation if historical
versions must remain usable. Public keys must arrive through trusted operator configuration;
never trust a key supplied by a version response.

The evidence level is `signed_registration_only`. The signature attests registration metadata.
It does **not** prove artifact bytes were fetched, provider credentials belong to the claimed app,
or the remote provider executed that immutable version. Consumers must retrieve and validate the
referenced execution snapshot and retain the existing provider revision/identity boundaries.

The canonical JSON algorithm is an owner serialization, not a claim of RFC 8785 compliance.
Consumers verify the supplied bytes, then compare parsed values. They must not reserialize the
payload with a different language's number formatting to validate the signature.

## HTTP interface

All routes use existing Nexus authentication. The privileged static operator key is global and
can select a configured Zone with `X-Nexus-Zone-ID`; deploy it only in trusted server processes.
An ordinary provider-authenticated credential cannot override its assigned Zone.

| Route | Purpose |
|---|---|
| `POST /api/v2/product-agents/commands` | RegisterPrincipal, PublishVersion, DecideVersion, MoveChannel, SetPrincipalStatus |
| `GET /api/v2/product-agents/{domain}/{name}/versions` | 50-item lexicographic pages, latest decision and current Channels |
| `GET /api/v2/product-agents/{domain}/{name}/versions/{version}` | Read one historical version and its latest decision |
| `GET /api/v2/product-agents/{domain}/{name}/resolve` | Resolve an exact approved version or stable/canary, default stable |

Commands require a request ID. Exact retries by the same authenticated actor return the saved
result; changed payloads or another actor reusing the ID conflict. A saved result is a historical
command acknowledgement, not current authorization: resolve again before execution.

Command bodies are limited to 64 KiB and reject unknown fields, duplicate JSON keys, non-finite
numbers and invalid Unicode. Error responses contain fixed codes, not submitted bodies or SQL.
The exported owner schema references the existing ResourceRef definition. OpenAPI exposes a
ResourceRef marker due to Pydantic's external-URN limitation; the ResourceRef runtime validator
is still enforced, and the exported contract schema resolves the marker to the canonical `$ref`.

## Explicit enablement

1. Back up the configured primary RecordStore. Keep legacy publishers active until a separate,
   reviewed consumer migration is ready; initializing these additive tables does not migrate them.
2. Set `NEXUS_AGENT_REGISTRY_DATABASE_URL` to the **same primary RecordStore database** and run:
   `python -m nexus.services.product_agent_registry --initialize-schema`.
3. Create an operator-managed Ed25519 private PEM key with file mode `0600` and configure
   `NEXUS_AGENT_REGISTRY_SIGNING_KEY_FILE`. No key is generated on startup.
4. Configure `NEXUS_AGENT_REGISTRY_DOMAIN_ZONES` as JSON, for example
   `{"agents.example.com":"organization-zone"}`. Authenticated server mode is required.
5. When rotating, optionally set `NEXUS_AGENT_REGISTRY_TRUST_FILE` to a protected JSON array of
   public PEM strings. The current signing key is always trusted, plus these historical keys.
6. Start the Nexus Python control-plane service using the existing deployment procedure and
   the primary RecordStore. Enabling an embedded Rust `nexusd` alone does not enable this API.

Normal startup verifies the tables already exist. It never copies/deletes legacy records, fences
legacy writers, or performs an authority cutover. A consumer migration must independently stop
old publishers, retain legacy IDs and snapshots, reconcile all records, and enable one authority.
The Registry must not be called as a second writer inside a consumer database transaction.

## Reproduction and limits

Generate/check owner schemas, TypeScript wire shapes, and the public-key-only conformance fixture:

```sh
python -m nexus.services.product_agent_registry.export_contract
python -m nexus.services.product_agent_registry.export_contract --check
pytest -n 0 tests/unit/services/test_product_agent_registry*.py
```

To repeat store/API tests on an isolated PostgreSQL test database, set
`NEXUS_REGISTRY_TEST_DATABASE_URL=postgresql+psycopg://...`. Each test creates and removes its own
schema. This variable is exclusively for tests; never point it at a production database.

SQLite tests do not establish multi-node HA. PostgreSQL transaction tests alone do not establish
application failover or production readiness. Moss authority migration, its management interface,
SudoWork consumption, deployment and real-provider end-to-end evidence are separate work packages.
