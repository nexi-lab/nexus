# Integration Tests

This directory contains integration tests that require external services like PostgreSQL.

## PostgreSQL Integration Tests

### Setup

Start PostgreSQL using docker-compose:

```bash
# From the nexus root directory
docker compose -f docker-compose.demo.yml up postgres -d
```

Or use the full demo stack:

```bash
./docker-demo.sh
```

### Running Tests

Run all PostgreSQL integration tests:

```bash
pytest tests/integration/test_auth_postgres.py -v
```

Run specific test:

```bash
pytest tests/integration/test_auth_postgres.py::test_oauth_race_condition_postgres -v
```

### Test Coverage

**`test_auth_postgres.py`**
- `test_oauth_race_condition_postgres`: Verifies PostgreSQL prevents duplicate API keys during concurrent OAuth callbacks
- `test_user_registration_postgres`: Basic smoke test for PostgreSQL user operations

### PostgreSQL Connection

Tests connect to:
- **Host**: localhost
- **Port**: 5432 (default) or 5433 (if configured differently)
- **Database**: nexus
- **User**: postgres
- **Password**: nexus

Connection string: `postgresql://postgres:nexus@localhost:5432/nexus`

### Cleanup

Stop PostgreSQL:

```bash
docker compose -f docker-compose.demo.yml down postgres
```

Or stop all services:

```bash
docker compose -f docker-compose.demo.yml down
```

## CI/CD Integration

These tests can be integrated into CI/CD pipelines by:

1. Starting PostgreSQL container in CI environment
2. Waiting for PostgreSQL to be ready
3. Running integration tests
4. Stopping and cleaning up containers

Example GitHub Actions workflow:

```yaml
name: Integration Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:15-alpine
        env:
          POSTGRES_DB: nexus
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: nexus
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -e .
      - run: pytest tests/integration/test_auth_postgres.py -v
```
