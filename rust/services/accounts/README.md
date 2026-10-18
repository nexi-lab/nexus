# Human account service

`nexus-accounts` implements registration, password and Google login, profile reads and updates, password changes, and email verification over the existing SQL `users` and `user_oauth_accounts` tables. The gRPC contract is [accounts.proto](../proto/nexus/accounts/v1/accounts.proto). HTTP invokes those same service methods and generated DTOs.

The service reads account IDs, bcrypt hashes, verification and activity flags, profile fields and zone fields from their existing rows. It creates no schema or session tables. PostgreSQL and SQLite execute the same parameterized queries. Password updates compare the original hash at the database write, so a concurrent password change cannot be overwritten.

Account JWTs retain the HS256 claim format used by `DatabaseLocalAuth`. Both transports check the current SQL account on every authenticated request; account state and administrator flags are not taken from cached browser metadata. Tokens have a bounded lifetime and no server-side session records. As with the existing stateless JWT contract, changing a password does not revoke previously issued account tokens; deactivating the account rejects them immediately.

New passwords pass through one `NewPassword` constructor before entering the bounded blocking pool. Registration and password changes share its eight-character minimum; validation examines at most eight Unicode characters. Hashing accepts only that validated type and uses bcrypt cost 12. Hashing and verification share the existing 72-byte input rule, including UTF-8 inputs that cross the byte limit. Cancelling a request keeps its admission permit with unfinished work.

## Run locally

Provide an existing account database in `NEXUS_DATABASE_URL` and its account signing secret in `NEXUS_JWT_SECRET`. The secret must contain at least 32 bytes. Credentials are read from the environment and are not CLI arguments.

SQLite locations use the existing SQLAlchemy URL convention: `sqlite:///relative.db`, `sqlite:////absolute/path/accounts.db` on Unix, or `sqlite:///C:/path/accounts.db` on Windows. `sqlite+pysqlite` uses the same rule. Startup never probes alternative paths or creates a missing database.

```sh
cargo run -p nexus-accounts --bin nexusd-accounts
```

The default listeners are `127.0.0.1:12015` for gRPC and `127.0.0.1:12016` for HTTP. The binary requires loopback listeners. A gateway owns external TLS and routing. Listener addresses are printed once as JSON on stdout; logs go to stderr.

| HTTP | gRPC |
| --- | --- |
| `POST /auth/register` | `AccountsService.Register` |
| `POST /auth/login` | `AccountsService.Login` |
| `GET /auth/me` | `AccountsService.GetProfile` |
| `PATCH /auth/me` | `AccountsService.UpdateProfile` |
| `POST /auth/change-password` | `AccountsService.ChangePassword` |
| `POST /auth/request-verification` | `AccountsService.RequestEmailVerification` |
| `POST /auth/verify-email` | `AccountsService.VerifyEmail` |
| `GET /auth/oauth/google/authorize` | `AccountsService.StartGoogleLogin` |
| `POST /auth/oauth/google/callback` | `AccountsService.FinishGoogleLogin` |

Profile and password requests carry `Authorization: Bearer <account JWT>`. All HTTP responses use `Cache-Control: no-store`. The native daemon continues to own file credentials and ReBAC grants. This component does not issue file API keys or treat account JWT claims as file grants.

## Registration

Registration accepts an email, a password of at least eight characters, and optional username/display name. Usernames are nonempty, at most 255 characters, and cannot contain `@`; existing usernames remain readable by Login. Email comparison retains the existing database's exact comparison. A new UUID identifies the account; administrator, zone, external identity and file credentials cannot be supplied by the caller.

The response is the unverified `UserProfile` (HTTP 201), without credentials. The browser separately requests an email verification link, then calls Login after verification. A failed delivery can be retried through RequestEmailVerification without registering again. Login refuses unverified accounts.

Identity checks and insertion share one short transaction: SQLite uses `BEGIN IMMEDIATE`, PostgreSQL uses `SERIALIZABLE`. PostgreSQL serialization conflicts retry the whole transaction at most three times, reusing the already computed bcrypt hash. Exhaustion returns Aborted/HTTP 409; duplicate active email or username returns AlreadyExists/HTTP 409. Hashing occurs before the transaction. No identity reservation or runtime lock is persisted, and historical duplicate accounts are preserved.

Password and Google creation share this transaction contract. Deployment must route every account writer through the Rust service when retiring Python routes; mixed writers with weaker isolation cannot provide this guarantee.

## Google login

Google login is optional. Configure these three environment variables together:

| Variable | Value |
| --- | --- |
| `NEXUS_ACCOUNTS_GOOGLE_CLIENT_ID` | The registered Google application's client ID |
| `NEXUS_ACCOUNTS_GOOGLE_CLIENT_SECRET` | Its client secret |
| `NEXUS_ACCOUNTS_GOOGLE_REDIRECT_URI` | The exact registered frontend callback URL, without credentials, query or fragment |

The callback requires HTTPS; HTTP is allowed on loopback for local acceptance. Without Google configuration its endpoints return 503. Invalid or partial configuration refuses startup. Enabling Google also requires the provisioned `user_oauth_accounts` schema. The production issuer is fixed to `https://accounts.google.com`.

StartGoogleLogin returns an authorization URL, signed ten-minute state and a private browser binding. HTTP places the binding in a host-only HttpOnly SameSite=Lax cookie, using Secure and the `__Host-` prefix for HTTPS. Code generation excludes that field from JSON serialization and deserialization; HTTP obtains it only from the cookie. The gateway must serve the account API over TLS on the same site as the frontend, and frontend calls must carry cookies. Starting another flow replaces the host's previous cookie. Every HTTP callback clears it, so a failed browser attempt starts a new flow.

The code flow uses S256 PKCE, nonce and a fixed redirect. The callback validates state signature, purpose, lifetime, client, redirect and cookie binding before exchange. OIDC validates the provider signature, issuer, audience, authorized party, nonce, expiration, issue time, `nbf`, verified email and optional access-token hash. Authorization codes are consumed once by the provider. State and verifier remain outside SQL and process caches, so another service instance can finish a flow after restart. Provider access/ID tokens are discarded after verification.

An existing `(google, subject)` binding keeps its user ID and edited account facts, including when Google reports a changed email. Disabled, deleted or unverified linked accounts are refused. New subjects never attach to an account by email: a collision returns 409 and requires the existing sign-in method. A new Google account is verified, non-admin and has no zone or file key. Account creation and subject binding commit together through the same transaction as password registration; only complete database transactions retry. Provider calls finish before the transaction and are never repeated by its retry loop.

Provider IO follows no redirects, uses HTTPS, caps responses at 128 KiB, and has three-second connection/five-second request timeouts. Up to eight exchanges run concurrently with a fifteen-second flow limit. Discovery/JWKS are cached for five minutes. Key misses can refresh immediately, then at most once per thirty seconds; refresh is shared across concurrent callers. An expired cache cannot authenticate through a failed refresh. See [Google's OIDC contract](https://developers.google.com/identity/openid-connect/openid-connect) and [openidconnect's validation API](https://docs.rs/openidconnect/4.0.1/openidconnect/).

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

CI runs the same binary journey against SQLite and an isolated PostgreSQL service. A loopback SMTP server receives actual MIME messages; the journey verifies registration through verification and login, Python-issued links in Rust and Rust-issued links in Python, restart, stale email rejection, and concurrent ownership conflicts. Two independent Rust processes race registration over HTTP and gRPC; PostgreSQL holds both inserts until both identity reads finish, exercising serialization conflicts without timing guesses. Distinct identities must both commit. Linux also asserts a clean SIGTERM exit. The PostgreSQL test URL must use loopback and a database name beginning with `nexus_accounts_test`; the runner refuses a nonempty database. Compatibility SQL and bcrypt fixtures were generated from `UserModel` and the Python password implementation.

Google network tests substitute only the external provider, which signs RSA ID tokens and enforces single-use codes and PKCE. They exercise actual account HTTP/gRPC, preserved legacy bindings, wrong claims/cookies, replica restart, concurrent Google/password creation, key rotation, expired caches and IO bounds. Set `ACCOUNTS_OAUTH_TEST_DATABASE_URL` to a loopback `nexus_accounts_test*` PostgreSQL database without query parameters to include PostgreSQL; each journey owns a temporary schema. Linux CI supplies that fixture. Windows CI runs the same Rust package and SQLite network journeys. The Python binary runner also verifies disabled/invalid Google configuration and checks both Rust SQL fixtures against the canonical account models. Live Google and complete frontend acceptance remain separate.

## Remaining integration

Live Google browser acceptance, explicit authenticated account linking, native file-credential exchange, gateway/frontend wiring and Python route removal remain unfinished. The browser verification page is part of that frontend work. The frontend account rollout stays draft until those pieces and their complete user journeys are ready. This service has not been wired into Tencent production deployment.
