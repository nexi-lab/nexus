# Nexus Agent Exchange Protocol

**Version:** 2026.1
**Status:** Stable
**Issue:** #1361

## Overview

The Nexus Agent Exchange Protocol defines a formal, versioned API surface for agent-to-agent economic operations. It covers three domains:

1. **Identity** — Cryptographic agent identity (Ed25519 keys, verification, rotation)
2. **Payment** — Credit transfers, reservations, metering, balance queries
3. **Audit** — Immutable transaction audit trail with integrity verification

## Authentication

All endpoints require authentication via one of three methods:

| Method | Header | Use Case |
|--------|--------|----------|
| API Key | `X-API-Key: nx_live_...` | Server-to-server |
| Bearer JWT | `Authorization: Bearer <token>` | User/agent sessions |
| x402 Payment | `X-402-Payment: <proof>` | External wallet operations |

## Versioning

### Protocol Version Header

Include `Nexus-Protocol-Version: 2026.1` in all requests. The server will respond with the same header indicating its supported version.

### Version Evolution Rules

1. **Additive changes** (new fields, new endpoints) do NOT bump the version
2. **Breaking changes** (field removal, type changes) bump the version
3. Proto field evolution: new fields get new field numbers; old field numbers are never reused
4. Deprecated fields are marked with `[deprecated = true]` in proto and `deprecated: true` in OpenAPI

### Compatibility

- Clients SHOULD send the `Nexus-Protocol-Version` header
- Servers MUST accept requests without the header (default to latest)
- Servers MUST reject requests with an unsupported major version

## Error Handling

All errors follow the google.rpc.Status pattern:

```json
{
  "error": {
    "code": "INSUFFICIENT_BALANCE",
    "message": "Agent agent-123 has insufficient balance for transfer",
    "details": {
      "available": "10.00",
      "required": "25.00"
    },
    "trace_id": "abc-123-def-456"
  }
}
```

### Error Code Ranges

| Range | Domain | Example |
|-------|--------|---------|
| 0-999 | General | `INVALID_ARGUMENT`, `NOT_FOUND`, `RATE_LIMITED` |
| 1000-1999 | Identity | `AGENT_NOT_FOUND`, `KEY_REVOKED`, `SIGNATURE_INVALID` |
| 2000-2999 | Payment | `INSUFFICIENT_BALANCE`, `TRANSFER_FAILED`, `INVALID_AMOUNT` |
| 3000-3999 | Audit | `RECORD_NOT_FOUND`, `INTEGRITY_VIOLATION` |
| 4000-4999 | Exchange | Reserved for future auction/settlement |

### HTTP Status Code Mapping

| Error Code | HTTP Status |
|-----------|-------------|
| `INVALID_ARGUMENT` | 400 |
| `UNAUTHENTICATED` | 401 |
| `INSUFFICIENT_BALANCE` | 402 |
| `PERMISSION_DENIED` | 403 |
| `NOT_FOUND` / `*_NOT_FOUND` | 404 |
| `ALREADY_EXISTS` | 409 |
| `KEY_REVOKED` / `*_EXPIRED` | 410 |
| `RATE_LIMITED` | 429 |
| `INTERNAL` / `*_FAILED` | 500 |

## Rate Limiting

All endpoints enforce rate limits. Limits are communicated via response headers:

| Header | Description |
|--------|-------------|
| `X-RateLimit-Limit` | Maximum requests per window |
| `X-RateLimit-Remaining` | Remaining requests in window |
| `X-RateLimit-Reset` | Seconds until limit resets |

When rate limited, the server responds with HTTP 429 and error code `RATE_LIMITED`.

## Pagination

List endpoints use cursor-based pagination:

```
GET /api/v2/audit/transactions?limit=50
→ { "transactions": [...], "next_cursor": "abc123", "has_more": true }

GET /api/v2/audit/transactions?limit=50&cursor=abc123
→ { "transactions": [...], "next_cursor": null, "has_more": false }
```

### Pagination Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `limit` | Items per page (1-1000) | 100 |
| `cursor` | Opaque cursor from previous response | (first page) |
| `include_total` | Include total count (opt-in, adds COUNT query) | false |

## Monetary Amounts

All monetary amounts are represented as **decimal strings** (e.g., `"10.50"`) to avoid floating-point precision issues. Maximum 6 decimal places.

Currency code `NXC` represents Nexus Credits (the internal unit of account).

## Proto-First Design

The protocol is defined proto-first:

```
proto/nexus/exchange/v1/
├── common.proto      # Shared types, error codes
├── identity.proto    # Agent identity messages
├── payment.proto     # Payment messages
├── audit.proto       # Audit messages
└── exchange.proto    # Service definition
```

The OpenAPI spec (`docs/protocol/nexus-exchange-v1.openapi.yaml`) is the REST transport binding. Proto messages define the canonical type system; Pydantic models in FastAPI are the runtime representation.

## Transport

**Phase 1 (current):** REST/JSON over HTTPS
**Phase 2 (planned):** Connect-RPC (binary protobuf + REST fallback)
**Phase 3 (planned):** gRPC for high-throughput inter-agent communication

## Conformance Testing

The protocol includes conformance tests using [schemathesis](https://github.com/schemathesis/schemathesis) that auto-generate test cases from the OpenAPI spec. See `tests/conformance/` for details.
