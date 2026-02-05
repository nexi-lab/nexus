# Disaster Recovery Runbook

Emergency procedures for Nexus server recovery.

## RTO/RPO Targets

| Scenario | RPO | RTO | Method |
|----------|-----|-----|--------|
| Database corruption | 5 min | 30 min | PITR from WAL |
| Full server failure | 24 hours | 1 hour | Full restore |
| CAS data loss | 24 hours | 2 hours | CAS restore |
| Accidental deletion | 0 | 15 min | Version history |

## Quick Reference

```bash
# Check health
curl http://localhost:2026/health

# View status
docker compose -f docker-compose.demo.yml ps

# List backups
./scripts/backup.sh --list

# Quick restore
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump
```

## Scenario 1: PostgreSQL Failure

```bash
# 1. Check logs
docker logs nexus-postgres --tail 100

# 2. Try restart
docker restart nexus-postgres

# 3. If corrupt, restore from backup
docker compose -f docker-compose.demo.yml down
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump
docker compose -f docker-compose.demo.yml up -d
```

## Scenario 2: CAS Corruption

```bash
# 1. Stop server
docker stop nexus-server

# 2. Backup corrupted CAS
mv nexus-data/cas nexus-data/cas.corrupted

# 3. Restore
./scripts/restore.sh --cas backups/daily/nexus-cas-latest.tar.gz

# 4. Restart
docker start nexus-server
```

## Scenario 3: Full Server Loss

```bash
# 1. Provision new server
# 2. Clone repo
git clone https://github.com/nexi-lab/nexus.git && cd nexus

# 3. Download backups from GCS
./scripts/restore.sh --from-gcs YYYYMMDD

# 4. Restore
./scripts/restore.sh --pg backups/downloaded/nexus-pg-*.dump
./scripts/restore.sh --cas backups/downloaded/nexus-cas-*.tar.gz

# 5. Start services
docker compose -f docker-compose.demo.yml up -d
```

## Post-Incident Checklist

- [ ] Timeline documented
- [ ] Root cause identified
- [ ] Backup verified working
- [ ] Runbook updated
- [ ] DR drill scheduled

## Key File Paths

| Component | Path |
|-----------|------|
| Backup script | `scripts/backup.sh` |
| Restore script | `scripts/restore.sh` |
| Docker Compose | `docker-compose.demo.yml` |
| PostgreSQL data | Docker volume: `nexus_postgres-data` |
| WAL archive | Docker volume: `nexus_postgres-wal-archive` |
| CAS storage | `nexus-data/cas/` |

## See Also

- [Backup and Recovery Guide](./backup-recovery.md)
