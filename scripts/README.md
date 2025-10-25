# Deployment Scripts

This directory contains scripts for deploying Nexus to production servers.

## Scripts

### `deploy-docker-image.sh` - PyPI Production Deployment

Deploys the **released version** from PyPI to production.

**Use this for:**
- ✅ Production deployments after PyPI release
- ✅ Deploying stable, tested versions
- ✅ Official releases

**Example:**
```bash
./scripts/deploy-docker-image.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

### `deploy-docker-local.sh` - Local Development Deployment

Builds from **local source code** and deploys for testing.

**Use this for:**
- ✅ Testing bug fixes before releasing
- ✅ Testing new features in production-like environment
- ✅ Rapid iteration during development
- ❌ NOT for production releases

**Example:**
```bash
# Build from current branch and deploy
./scripts/deploy-docker-local.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"

# Custom tag for this test
./scripts/deploy-docker-local.sh --tag fix/fuse-remote-metadata
```

## Workflow

### Testing a Bug Fix

1. Create a feature branch with your fix
2. Test locally
3. Deploy to staging using local build:
   ```bash
   ./scripts/deploy-docker-local.sh --tag my-bugfix
   ```
4. Verify the fix works in production environment
5. Merge to main
6. Release to PyPI
7. Deploy to production:
   ```bash
   ./scripts/deploy-docker-image.sh
   ```

### Production Release

1. Ensure all tests pass
2. Update version in `pyproject.toml`
3. Release to PyPI (see CLAUDE.md)
4. Deploy using PyPI script:
   ```bash
   ./scripts/deploy-docker-image.sh
   ```

## Options

Both scripts support:

- `--project-id` - GCP project (default: nexi-lab-888)
- `--instance-name` - VM name (default: nexus-server)
- `--zone` - GCP zone (default: us-west1-a)
- `--port` - Server port (default: 8080)
- `--cloud-sql-instance` - PostgreSQL Cloud SQL instance
- `--db-name` - Database name (default: nexus)
- `--db-user` - Database user (default: postgres)
- `--db-password` - Database password

Additional options for `deploy-docker-local.sh`:

- `--tag` - Custom Docker image tag
- `--skip-build` - Use existing image without rebuilding

## Quick Reference

| Task | Script | Command |
|------|--------|---------|
| Test local changes | `deploy-docker-local.sh` | `./scripts/deploy-docker-local.sh` |
| Production release | `deploy-docker-image.sh` | `./scripts/deploy-docker-image.sh` |
| Custom tag | `deploy-docker-local.sh` | `--tag my-feature` |
| Skip rebuild | `deploy-docker-local.sh` | `--skip-build` |
| With PostgreSQL | Either | `--cloud-sql-instance ...` |
| GCS only | Either | (omit Cloud SQL options) |

## Architecture

Both scripts deploy with:

- **Storage Backend:** GCS (`nexi-hub` bucket)
- **Metadata Store:** PostgreSQL (Cloud SQL, optional)
- **Networking:** Host network mode
- **Ports:** 80 (public) → 8080 (container)
- **Authentication:** Via GCP metadata service

Docker images are stored in GCR:
- PyPI builds: `gcr.io/nexi-lab-888/nexus-server:latest`
- Local builds: `gcr.io/nexi-lab-888/nexus-server:local-TIMESTAMP`
