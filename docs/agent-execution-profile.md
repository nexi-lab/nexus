# Agent task identity binding (experimental)

This profile binds a product Agent asset and immutable version to a short-lived **real Nexus IAM delegation**. It does not introduce a token format, replace IAM, or claim that a registration signature proves execution isolation.

## Authority and provisioning

An operator explicitly initializes the additive Registry tables with `python -m nexus.services.product_agent_registry --initialize-schema`, registers a coordinator through the existing Agent registration service, and configures `NEXUS_AGENT_EXECUTION_POLICY_FILE`. Startup and GET never provision identities or migrate old data. The coordinator must exist in the same zone with the configured owner. A task worker is an IAM identity, not a runtime PID.

The policy maps zone IDs to `ExecutionZonePolicy`: coordinator identity/owner, authenticated `issuers`, `evaluators`, `gateways`, production `permissions`, and separate `evaluation_permissions`. A permission is an exact `(operation, resource)` pair. Resources are administrator-managed resolver IDs; they must also appear in reviewed asset metadata. They are not caller-selected URLs or credentials. Configure evaluation resources to use isolated test datasets and endpoints.

`initiated_by` is human provenance asserted by the trusted Moss gateway. This service does **not** claim user-to-Agent IAM delegation. Browser fields cannot grant issuer authority. Gateway callers must authenticate with Nexus and match the zone policy.

## API

All routes are under `/api/v2/product-agents/execution` and return `Cache-Control: no-store`:

- `POST /grants`: reserve a durable binding, issue an IAM CLEAN delegation with no VFS grants and no sub-delegation, then activate. TTL is 1–300 seconds. The raw API key is returned once and never stored in the binding table.
- `GET /grants?asset_id=…&after=…`: paginated recovery inventory, including interrupted issuance; no secrets.
- `GET /grants/{id}`: inspect current binding; no writes.
- `POST /authorize`: authenticated gateway submits the delegated token in the body, exact task/spec/permission. The token never appears in a URL or validation error.
- `POST /grants/{id}/revoke`: persist local denial first, then revoke actual IAM credentials/grants. Cleanup failures remain visible and retryable.

Production issuance requires reviewed asset metadata and fresh Registry resolution of an approved, unexpired Version whose spec digest matches. Each authorization repeats those checks, validates the real key/zone junction/TTL, actual delegation status and exact scope, and rechecks local revocation. `cacheable:false` decisions must not be reused for another call. A gateway must check immediately before each side effect; this is a point-in-time decision, not cancellation of a remote operation already dispatched.

Evaluation issuance requires a separately configured evaluator and test-resource allowlist, approved asset registration, metadata/spec/dataset digests, and no production version selector. An evaluation grant does not publish, promote, or raise the asset's L0/L1/L2 grade.

## Recovery and revocation

The `(zone, issuer, request_id)` reservation prevents duplicate workers during concurrent retries. A replay returns metadata without the original key; a client that lost its response must recover/revoke that binding before an explicit replacement. Pending and failed grants never authorize.

A unique worker ID recovers IAM state after a crash before activation. The delegation ID is persisted before cleanup. The IAM owner now permits retrying cleanup after a revoke failure and cleanup of completed/expired delegations without rewriting their outcomes. Concurrent revoke during issuance cannot reactivate the binding, including the interval between the IAM record and key creation.

The binding retains issuer, human provenance, owner, department, task, digests, permission scope, timestamps, and stopping actor. It stores no model cost. Cost attribution requires separate trusted Router ledger evidence.

## Validation and remaining integration

Tests use actual SQLite, Product Registry, AgentRegistrationService, DelegationService, ReBACManager and DatabaseAPIKeyAuth. HTTP tests use real database credentials. Fault injection is confined to interruption/cleanup boundaries. Tests cover concurrent issuance, one-time secrets, zone/task/scope isolation, version revocation, expired keys, missing zone membership, asset suspension, terminal delegation cleanup and restart recovery. These are not evidence of PostgreSQL HA or production Dify/LangGraph L2 acceptance.

Moss task/model/tool dispatch integration, real Router attribution and hosted runtime/evaluation acceptance remain separate implementation steps. This profile alone does not complete Agent Management 1.1.
