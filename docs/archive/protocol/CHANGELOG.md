# Nexus Exchange Protocol Changelog

All notable changes to the Exchange Protocol specification are documented here.

## Versioning

The protocol uses calendar versioning: `YYYY.N` where `YYYY` is the year and `N` is the release number within that year.

- **Additive changes** (new fields, new endpoints): No version bump required
- **Breaking changes** (field removal, type changes): Version bump required
- **Proto field numbers**: Never reused after removal

## [2026.1] — 2026-02-12

### Added
- Initial protocol specification (Issue #1361)
- Proto message definitions for Identity, Payment, and Audit domains
- OpenAPI 3.1 spec with full endpoint documentation
- NexusErrorCode enum with domain-specific error codes (0-4999)
- Structured error responses following google.rpc.Status pattern
- `Nexus-Protocol-Version` header for version negotiation
- Conformance test suite using schemathesis
- CI workflow for proto linting and breaking change detection
- buf toolchain configuration for proto-first development

### Endpoints
- **Identity**: verify, list keys, rotate key, revoke key
- **Payment**: balance, can-afford, transfer, batch transfer, reserve, commit, release, meter
- **Audit**: list transactions, get transaction, aggregations, export, integrity verification

### Proto Files
- `proto/nexus/exchange/v1/common.proto` — Shared types and error codes
- `proto/nexus/exchange/v1/identity.proto` — Agent identity messages
- `proto/nexus/exchange/v1/payment.proto` — Payment operation messages
- `proto/nexus/exchange/v1/audit.proto` — Audit trail messages
- `proto/nexus/exchange/v1/exchange.proto` — Service definition
