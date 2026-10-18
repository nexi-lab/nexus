# Human account service

`nexus-accounts` implements password login, profile reads and updates, and password changes over the existing SQL `users` table. The gRPC contract is [accounts.proto](../proto/nexus/accounts/v1/accounts.proto). HTTP invokes those same service methods.

The service reads account IDs, bcrypt hashes, verification and activity flags, profile fields and zone membership from their existing rows. It creates no schema or session tables. PostgreSQL and SQLite execute the same parameterized queries. Password updates compare the original hash at the database write, so a concurrent password change cannot be overwritten.

Account JWTs retain the HS256 claim format used by `DatabaseLocalAuth`. Both transports check the current SQL account on every authenticated request; account state and administrator flags are not taken from cached browser metadata. Tokens have a bounded lifetime and no server-side session records. As with the existing stateless JWT contract, changing a password does not revoke previously issued account tokens; deactivating the account rejects them immediately.

Password work uses a bounded blocking pool. Cancelling a request keeps its admission permit with unfinished work. Hashing retains bcrypt cost 12 and the existing 72-byte password rule, including UTF-8 inputs that cross the byte limit.

## Run locally

Provide an existing account database in `NEXUS_DATABASE_URL` and its account signing secret in `NEXUS_JWT_SECRET`. The secret must contain at least 32 bytes. Credentials are read from the environment and are not CLI arguments.

SQLite locations use the existing SQLAlchemy URL convention: `sqlite:///relative.db`, `sqlite:////absolute/path/accounts.db` on Unix, or `sqlite:///C:/path/accounts.db` on Windows. `sqlite+pysqlite` uses the same rule. Startup never probes alternative paths or creates a missing database.

```sh
cargo run -p nexus-accounts --bin nexusd-accounts
```

The default listeners are `127.0.0.1:12015` for gRPC and `127.0.0.1:12016` for HTTP. The binary requires loopback listeners. A gateway owns external TLS and routing. Listener addresses are printed once as JSON on stdout; logs go to stderr.

| HTTP | gRPC |
| --- | --- |
| `POST /auth/login` | `AccountsService.Login` |
| `GET /auth/me` | `AccountsService.GetProfile` |
| `PATCH /auth/me` | `AccountsService.UpdateProfile` |
| `POST /auth/change-password` | `AccountsService.ChangePassword` |

Profile and password requests carry `Authorization: Bearer <account JWT>`. All HTTP responses use `Cache-Control: no-store`. The native daemon continues to own file credentials and ReBAC grants. This component does not issue file API keys or treat account JWT claims as file grants.

## Acceptance

```sh
cargo test --locked -p nexus-accounts
cargo clippy --locked -p nexus-accounts --all-targets -- -D warnings
python scripts/test_rust_accounts.py \
  --binary target/debug/nexusd-accounts \
  --output-dir /tmp/accounts-sqlite
```

On Windows, use `target/debug/nexusd-accounts.exe`. The Python journey needs the repository's Python dependencies and `grpcio-tools==1.80.0`. It creates accounts and tokens using the Python implementation, starts the real Rust binary, crosses both protocols, verifies Rust password writes with Python bcrypt, restarts, and checks deactivation and the absence of session tables. Rust network tests also cover invalid signatures and algorithms, cross-account claims, unknown mutation fields, ambiguous identifiers and database failures.

CI runs the same binary journey against SQLite and an isolated PostgreSQL service. The PostgreSQL test URL must use loopback and a database name beginning with `nexus_accounts_test`; the runner refuses a nonempty database. Compatibility SQL and bcrypt fixtures were generated from `UserModel` and the Python password implementation.

## Remaining integration

Registration, email verification, Google OAuth, native file-credential exchange, gateway/frontend wiring and Python route removal remain unfinished. The frontend account rollout stays draft until those pieces and their complete user journeys are ready. This service has not been wired into Tencent production deployment.
