# Human account service

`nexus-accounts` implements password login, profile reads and updates, password changes, and email verification over the existing SQL `users` table. The gRPC contract is [accounts.proto](../proto/nexus/accounts/v1/accounts.proto). HTTP invokes those same service methods.

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
| `POST /auth/request-verification` | `AccountsService.RequestEmailVerification` |
| `POST /auth/verify-email` | `AccountsService.VerifyEmail` |

Profile and password requests carry `Authorization: Bearer <account JWT>`. All HTTP responses use `Cache-Control: no-store`. The native daemon continues to own file credentials and ReBAC grants. This component does not issue file API keys or treat account JWT claims as file grants.

## Verification email

Configure all three variables to enable delivery:

| Variable | Value |
| --- | --- |
| `NEXUS_ACCOUNTS_SMTP_URL` | `smtps://smtp.example.com:465`, or `smtp://smtp.example.com:587` for required STARTTLS |
| `NEXUS_ACCOUNTS_EMAIL_FROM` | Sender mailbox |
| `NEXUS_ACCOUNTS_VERIFICATION_URL` | Absolute HTTPS URL of the browser verification page, without query or fragment |

SMTP credentials use `NEXUS_ACCOUNTS_SMTP_USERNAME` and `NEXUS_ACCOUNTS_SMTP_PASSWORD`; configure both or neither. URLs cannot contain credentials. Plain SMTP and HTTP verification pages are accepted only on loopback for local acceptance. TLS certificate verification stays enabled for remote SMTP. The transport uses [Lettre's required TLS modes](https://docs.rs/lettre/0.11.23/lettre/transport/smtp/struct.AsyncSmtpTransport.html).

Without delivery configuration, password/profile operations and verification of previously issued links remain available. Requesting a new link returns 503 for every identity. Partial or invalid delivery configuration refuses startup. Verification tokens are never logged or returned by the request endpoint.

`RequestEmailVerification` accepts an optional `email`. A configured service returns the same acceptance message for unknown, already verified, ambiguous, and eligible addresses; HTTP status is 202. It admits at most eight in-memory tasks before looking up identity, with a 15-second limit per task. Overload returns 429 before identity lookup. Acceptance does not guarantee delivery, and restart can discard unfinished requests; there is no persistent delivery queue. SMTP errors produce a redacted warning. SIGINT and Linux SIGTERM stop both listeners and drain admitted tasks before closing the database.

Links use the existing HS256 `email_verify` format and a 24-hour lifetime, independently of the account JWT lifetime. Verification atomically checks immutable user ID, current email, activity and deletion state. Replaying a valid link is idempotent and preserves the original mutation timestamp. The existing unique index prevents two active accounts from verifying the same email. Conflicting identities are never merged. Verification tokens cannot authenticate account operations.

## Acceptance

```sh
cargo test --locked -p nexus-accounts
cargo clippy --locked -p nexus-accounts --all-targets -- -D warnings
python scripts/test_rust_accounts.py \
  --binary target/debug/nexusd-accounts \
  --output-dir /tmp/accounts-sqlite
```

On Windows, use `target/debug/nexusd-accounts.exe`. The Python journey needs the repository's Python dependencies and `grpcio-tools==1.80.0`. It creates accounts and tokens using the Python implementation, starts the real Rust binary, crosses both protocols, verifies Rust password writes with Python bcrypt, restarts, and checks deactivation and the absence of session tables. Rust network tests also cover invalid signatures and algorithms, cross-account claims, unknown mutation fields, ambiguous identifiers and database failures.

CI runs the same binary journey against SQLite and an isolated PostgreSQL service. A loopback SMTP server receives actual MIME messages; the journey verifies Python-issued links in Rust and Rust-issued links in Python, restart, stale email rejection, and concurrent ownership conflicts. Linux also asserts a clean SIGTERM exit. The PostgreSQL test URL must use loopback and a database name beginning with `nexus_accounts_test`; the runner refuses a nonempty database. Compatibility SQL and bcrypt fixtures were generated from `UserModel` and the Python password implementation.

## Remaining integration

Registration, Google OAuth, native file-credential exchange, gateway/frontend wiring and Python route removal remain unfinished. The browser verification page is part of that frontend work. The frontend account rollout stays draft until those pieces and their complete user journeys are ready. This service has not been wired into Tencent production deployment.
