# Backup and Recovery

**Comprehensive guide to Nexus backup strategies and data recovery**

## What You'll Learn

- Configure automated PostgreSQL backups with retention policies
- Enable point-in-time recovery (PITR) with WAL archiving
- Back up CAS (Content-Addressable Storage) to cloud storage
- Perform full and partial restores
- Test and verify backup integrity

## Prerequisites

- Nexus server running (Docker or native)
- PostgreSQL 18+ with pg_dump access
- (Optional) GCS bucket for offsite backups
- (Optional) gsutil CLI for cloud uploads

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Nexus Backup Architecture                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │  PostgreSQL  │     │  WAL Archive │     │     CAS      │     │
│  │   Database   │────▶│   (PITR)     │     │   Storage    │     │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘     │
│         │                    │                    │              │
│         ▼                    ▼                    ▼              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                   backup.sh                               │   │
│  │   • Daily pg_dump (compressed)                           │   │
│  │   • WAL archive sync                                     │   │
│  │   • CAS tarball                                          │   │
│  └──────────────────────────┬───────────────────────────────┘   │
│                             │                                    │
│         ┌───────────────────┼───────────────────┐               │
│         ▼                   ▼                   ▼               │
│  ┌────────────┐     ┌────────────┐     ┌────────────────┐       │
│  │   Local    │     │   Weekly   │     │    GCS/S3      │       │
│  │  (Daily)   │     │  (Rotate)  │     │   (Offsite)    │       │
│  └────────────┘     └────────────┘     └────────────────┘       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Step 1: Configure Automated Backups

### Basic Setup

The backup script is located at `scripts/backup.sh`. First, make it executable:

```bash
chmod +x scripts/backup.sh
```

### Run a Manual Backup

```bash
# Full backup (PostgreSQL + CAS)
./scripts/backup.sh

# PostgreSQL only
./scripts/backup.sh --pg-only

# CAS data only
./scripts/backup.sh --cas-only

# Dry run (show what would be done)
./scripts/backup.sh --dry-run
```

### Configuration Options

Set environment variables to customize backup behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_BACKUP_DIR` | `./backups` | Local backup directory |
| `NEXUS_GCS_BACKUP_BUCKET` | (none) | GCS bucket for cloud backups |
| `NEXUS_BACKUP_RETENTION_DAYS` | `30` | Days to keep daily backups |
| `NEXUS_BACKUP_RETENTION_WEEKLY` | `4` | Number of weekly backups to keep |
| `NEXUS_BACKUP_RETENTION_MONTHLY` | `3` | Number of monthly backups to keep |
| `POSTGRES_CONTAINER` | `nexus-postgres` | Docker container name |
| `NEXUS_DATA_DIR` | `./nexus-data` | CAS data directory |

### Example: Production Configuration

```bash
export NEXUS_BACKUP_DIR=/data/backups
export NEXUS_GCS_BACKUP_BUCKET=my-nexus-backups
export NEXUS_BACKUP_RETENTION_DAYS=30

./scripts/backup.sh --gcs
```

## Step 2: Enable WAL Archiving

WAL (Write-Ahead Logging) archiving enables point-in-time recovery (PITR), allowing you to restore to any moment in time.

### Docker Compose Configuration

WAL archiving is pre-configured in `docker-compose.demo.yml`:

```yaml
postgres:
  command: >
    postgres
    -c archive_mode=on
    -c archive_command='test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
    -c archive_timeout=300
    -c wal_compression=lz4
  volumes:
    - postgres-wal-archive:/var/lib/postgresql/wal_archive
```

### Verify WAL Archiving

```bash
# Check archiver status
docker exec nexus-postgres psql -U postgres -c "SELECT * FROM pg_stat_archiver;"

# Check configuration
docker exec nexus-postgres psql -U postgres -c "SELECT name, setting FROM pg_settings WHERE name LIKE 'archive%';"

# Use the helper function
docker exec nexus-postgres psql -U postgres -c "SELECT * FROM nexus_check_wal_archive_status();"
```

**Expected output:**

| setting_name | setting_value | status |
|-------------|---------------|--------|
| archive_mode | on | OK |
| archive_command | test ! -f ... | OK |
| wal_level | replica | OK |

### WAL Archive Backup

The backup script automatically syncs WAL files:

```bash
# Backup includes WAL archive
./scripts/backup.sh

# WAL files are stored in
ls -la backups/wal/
```

## Step 3: Back Up CAS Storage to Cloud

### Local CAS Backup

```bash
# Backup CAS only
./scripts/backup.sh --cas-only

# View backup
ls -la backups/daily/nexus-cas-*.tar.gz
```

### Upload to GCS

```bash
# Set GCS bucket
export NEXUS_GCS_BACKUP_BUCKET=my-nexus-backups

# Full backup with GCS upload
./scripts/backup.sh --gcs
```

### Manual GCS Sync

```bash
# Sync backup directory to GCS
gsutil -m rsync -r ./backups gs://my-nexus-backups/backups/

# List remote backups
gsutil ls -l gs://my-nexus-backups/backups/
```

## Step 4: Verify Backup Integrity

### Automatic Verification

The backup script automatically:
1. Creates SHA-256 checksums for each backup
2. Validates PostgreSQL dumps can be listed

```bash
# Verify last backup
./scripts/backup.sh --verify
```

### Manual Verification

```bash
# Check checksum
sha256sum -c backups/daily/nexus-pg-latest.dump.sha256

# List PostgreSQL backup contents
gunzip -c backups/daily/nexus-pg-latest.dump | pg_restore --list

# Verify tarball integrity
tar -tzf backups/daily/nexus-cas-latest.tar.gz | head -20
```

## Step 5: Restore from Backup

### PostgreSQL Restore

```bash
# Make restore script executable
chmod +x scripts/restore.sh

# Restore PostgreSQL from backup
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump
```

**Warning:** This will DROP and recreate the database. All existing data will be lost.

### CAS Restore

```bash
# Restore CAS data
./scripts/restore.sh --cas backups/daily/nexus-cas-latest.tar.gz
```

### Point-in-Time Recovery (PITR)

Restore to a specific moment in time:

```bash
# Restore to specific timestamp
./scripts/restore.sh --pitr '2024-01-15 14:30:00'
```

**Note:** PITR requires WAL archiving to be enabled and WAL files available.

### Restore from GCS

```bash
# Download and list backups from a specific date
./scripts/restore.sh --from-gcs 20240115

# Then restore
./scripts/restore.sh --pg backups/downloaded/nexus-pg-20240115-020000.sql.gz
```

## Step 6: Test Recovery Procedures

**Important:** Regularly test your backups by performing test restores.

### Test Restore (Non-Destructive)

```bash
# Test restore to a temporary database
./scripts/restore.sh --test backups/daily/nexus-pg-latest.dump
```

This creates a temporary database, restores the backup, validates tables, then drops the test database.

### Full DR Drill Checklist

1. [ ] Download latest backup from GCS
2. [ ] Verify checksum
3. [ ] Test restore to staging environment
4. [ ] Verify application functionality
5. [ ] Document any issues
6. [ ] Update recovery time estimates

## Automated Backup Schedule

### Cron Configuration

Copy the example cron configuration:

```bash
sudo cp configs/backup-cron.example /etc/cron.d/nexus-backup
```

Or add to crontab:

```bash
crontab -e
```

```cron
# Daily PostgreSQL backup at 2 AM
0 2 * * * /opt/nexus/scripts/backup.sh --pg-only >> /var/log/nexus-backup.log 2>&1

# Weekly full backup (Sunday 3 AM)
0 3 * * 0 /opt/nexus/scripts/backup.sh >> /var/log/nexus-backup.log 2>&1

# Daily cleanup of old backups (5 AM)
0 5 * * * /opt/nexus/scripts/backup.sh --cleanup >> /var/log/nexus-backup.log 2>&1

# Upload to GCS (6 AM)
0 6 * * * /opt/nexus/scripts/backup.sh --gcs >> /var/log/nexus-backup.log 2>&1
```

### Systemd Timer (Alternative)

For systemd-based systems, create `/etc/systemd/system/nexus-backup.timer`:

```ini
[Unit]
Description=Nexus Daily Backup

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with:

```bash
sudo systemctl enable nexus-backup.timer
sudo systemctl start nexus-backup.timer
```

## Troubleshooting

### Backup Fails: "Connection refused"

**Problem:** Cannot connect to PostgreSQL.

**Solution:**
```bash
# Check if container is running
docker ps | grep nexus-postgres

# Check logs
docker logs nexus-postgres

# Verify connection
docker exec nexus-postgres pg_isready
```

### Backup Fails: "Permission denied"

**Problem:** Cannot write to backup directory.

**Solution:**
```bash
# Fix permissions
sudo chown -R $(whoami) ./backups
chmod 755 ./backups
```

### WAL Archive Not Working

**Problem:** `pg_stat_archiver` shows no archived files.

**Solution:**
```bash
# Check archive directory permissions
docker exec nexus-postgres ls -la /var/lib/postgresql/wal_archive/

# Check archive command manually
docker exec nexus-postgres bash -c "echo test > /var/lib/postgresql/wal_archive/test"

# Force a WAL switch
docker exec nexus-postgres psql -U postgres -c "SELECT pg_switch_wal();"
```

### Restore Fails: "Database in use"

**Problem:** Cannot drop database because connections exist.

**Solution:**
```bash
# Stop Nexus server first
docker stop nexus-server

# Then restore
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump
```

### Large Backups Taking Too Long

**Problem:** Backup duration is excessive.

**Solution:**
```bash
# Use parallel compression (if pigz available)
export GZIP_CMD="pigz -p 4"

# Or use incremental backups
./scripts/backup.sh --pg-only  # Only PostgreSQL changes frequently
```

## Best Practices

1. **Test restores regularly** - At least monthly, perform a full restore to staging
2. **Monitor backup success** - Set up alerts for failed backups
3. **Offsite backups** - Always maintain a copy in a different region/provider
4. **Encrypt sensitive backups** - Use GPG for backups containing secrets
5. **Document recovery procedures** - Keep runbooks updated
6. **Verify checksums** - Always verify before restore
7. **Retention policy** - Balance storage costs with recovery needs

## What's Next?

- [Disaster Recovery Runbook](./disaster-recovery-runbook.md) - Emergency procedures
- [PostgreSQL Configuration](../deployment/postgresql.md) - Database tuning
- [Server Setup](../deployment/server-setup.md) - Production deployment
