# Backup and Recovery

Comprehensive guide to Nexus backup strategies and data recovery.

## Quick Start

```bash
# Run a backup
./scripts/backup.sh

# Verify backup
./scripts/backup.sh --verify

# Restore from backup
./scripts/restore.sh --pg ./backups/daily/nexus-pg-latest.dump
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_BACKUP_DIR` | `./backups` | Backup directory |
| `NEXUS_GCS_BACKUP_BUCKET` | - | GCS bucket for cloud backups |
| `NEXUS_BACKUP_RETENTION_DAYS` | `30` | Days to keep daily backups |
| `POSTGRES_CONTAINER` | `nexus-postgres` | Docker container name |

## Backup Commands

```bash
./scripts/backup.sh                # Full backup (PostgreSQL + CAS)
./scripts/backup.sh --pg-only      # PostgreSQL only
./scripts/backup.sh --cas-only     # CAS data only
./scripts/backup.sh --gcs          # Upload to GCS
./scripts/backup.sh --verify       # Verify last backup
./scripts/backup.sh --cleanup      # Remove old backups
./scripts/backup.sh --list         # List backups
./scripts/backup.sh --dry-run      # Show configuration
```

## Restore Commands

```bash
./scripts/restore.sh --pg <file>           # Restore PostgreSQL
./scripts/restore.sh --cas <file>          # Restore CAS
./scripts/restore.sh --test <file>         # Test restore (non-destructive)
./scripts/restore.sh --pitr <timestamp>    # Point-in-time recovery
./scripts/restore.sh --from-gcs <date>     # Download from GCS
./scripts/restore.sh --list                # List backups
```

## WAL Archiving

WAL archiving is configured in `docker-compose.demo.yml` for point-in-time recovery:

```yaml
postgres:
  command: >
    postgres
    -c archive_mode=on
    -c archive_command='test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
    -c archive_timeout=300
```

Verify WAL archiving:

```bash
docker exec nexus-postgres psql -U postgres -c "SELECT * FROM nexus_check_wal_archive_status();"
```

## Automated Backups

Copy the cron configuration:

```bash
sudo cp configs/backup-cron.example /etc/cron.d/nexus-backup
```

## Backup Structure

```
backups/
├── daily/           # 30-day retention
│   ├── nexus-pg-YYYYMMDD-HHMMSS.dump
│   ├── nexus-pg-latest.dump -> (symlink)
│   ├── nexus-cas-YYYYMMDD-HHMMSS.tar.gz
│   └── nexus-cas-latest.tar.gz -> (symlink)
├── weekly/          # 4-week retention
├── monthly/         # 3-month retention
└── wal/             # WAL archive
```

## See Also

- [Disaster Recovery Runbook](./disaster-recovery-runbook.md)
