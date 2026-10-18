# Account fixtures

`users.sql` and `oauth-accounts.sql` are SQLite DDL generated from the canonical
SQLAlchemy account models. PostgreSQL network tests use the same DDL with its
timestamp type. These files do not migrate or provision production databases.

The two Google RSA private keys and their JWKs were generated for the loopback
OIDC test provider. They are public test data and must never be used by a deployed
service. The production binary fixes the issuer to Google and has no fixture
provider configuration.
