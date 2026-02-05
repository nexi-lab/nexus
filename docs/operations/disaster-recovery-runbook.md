# Disaster Recovery Runbook

**Emergency procedures for Nexus server recovery**

**For Use During:** Production incidents, data loss events, infrastructure failures

## RTO/RPO Targets

| Scenario | RPO (Max Data Loss) | RTO (Max Downtime) | Recovery Method |
|----------|---------------------|-------------------|-----------------|
| Database corruption | 5 minutes | 30 minutes | PITR from WAL archive |
| Full server failure | 24 hours | 1 hour | Full restore from backup |
| CAS data loss | 24 hours | 2 hours | CAS restore from backup |
| Accidental deletion | 0 (versioned) | 15 minutes | Version history / undelete |
| Region-wide outage | 24 hours | 4 hours | Cross-region restore |
| Ransomware/Security | 24 hours | 2 hours | Clean restore + audit |

**Definitions:**
- **RPO (Recovery Point Objective):** Maximum acceptable data loss measured in time
- **RTO (Recovery Time Objective):** Maximum acceptable downtime

## Emergency Contacts

| Role | Contact | Escalation Time |
|------|---------|-----------------|
| On-call Engineer | [TBD - Add contact] | Immediate |
| Database Admin | [TBD - Add contact] | 15 minutes |
| Infrastructure Lead | [TBD - Add contact] | 30 minutes |
| Security Team | [TBD - Add contact] | Immediate (security incidents) |

## Quick Reference Commands

```bash
# Check service health
curl http://localhost:2026/health

# View container status
docker compose -f docker-compose.demo.yml ps

# View logs
docker logs nexus-server --tail 100

# Emergency stop
docker compose -f docker-compose.demo.yml down

# List available backups
./scripts/backup.sh --list

# Quick restore
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump
```

---

## Scenario 1: PostgreSQL Database Failure

### Symptoms
- Health check fails: `curl http://localhost:2026/health` returns error
- Container restart loops
- "Connection refused" or "FATAL: database does not exist" errors
- Slow queries or timeouts

### Diagnosis

```bash
# 1. Check container status
docker ps -a | grep postgres

# 2. View PostgreSQL logs
docker logs nexus-postgres --tail 200

# 3. Check disk space
df -h

# 4. Check database connectivity
docker exec nexus-postgres pg_isready -U postgres

# 5. Check for corruption
docker exec nexus-postgres psql -U postgres -c "SELECT datname, pg_database_size(datname) FROM pg_database;"
```

### Recovery Steps

#### Option A: Restart (Minor Issues)

```bash
# 1. Restart PostgreSQL
docker restart nexus-postgres

# 2. Wait for healthy status
sleep 30

# 3. Verify
docker exec nexus-postgres pg_isready -U postgres
```

#### Option B: PITR Recovery (Data Corruption)

```bash
# 1. Stop all services
docker compose -f docker-compose.demo.yml down

# 2. Identify corruption time from logs
docker logs nexus-postgres 2>&1 | grep -i "error\|fatal" | tail -20

# 3. Restore to point before corruption
./scripts/restore.sh --pitr '2024-01-15 14:00:00'

# 4. Restart services
docker compose -f docker-compose.demo.yml up -d

# 5. Verify data integrity
docker exec nexus-postgres psql -U postgres -d nexus -c "SELECT COUNT(*) FROM file_paths;"
```

#### Option C: Full Restore (Complete Failure)

```bash
# 1. Stop all services
docker compose -f docker-compose.demo.yml down

# 2. Remove corrupted volume (DESTRUCTIVE)
docker volume rm nexus_postgres-data

# 3. Restore from backup
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump

# 4. Restart services
docker compose -f docker-compose.demo.yml up -d

# 5. Run migrations if needed
docker exec nexus-server python -m alembic upgrade head
```

### Verification

```bash
# Check table counts
docker exec nexus-postgres psql -U postgres -d nexus -c "
SELECT 'file_paths' as table_name, COUNT(*) as count FROM file_paths
UNION ALL
SELECT 'memories', COUNT(*) FROM memories
UNION ALL
SELECT 'rebac_tuples', COUNT(*) FROM rebac_tuples;
"

# Test API endpoint
curl -s http://localhost:2026/health | jq
```

---

## Scenario 2: CAS Storage Corruption

### Symptoms
- Files return "Content not found" errors
- Hash mismatch errors in logs
- Missing content blobs

### Diagnosis

```bash
# 1. Check CAS directory
ls -la nexus-data/cas/

# 2. Count files
find nexus-data/cas -type f | wc -l

# 3. Check for filesystem errors
dmesg | grep -i "error\|fault"

# 4. Verify a specific hash
# (Get hash from error log, then check if file exists)
ls nexus-data/cas/ab/cd/abcd1234...
```

### Recovery Steps

```bash
# 1. Stop Nexus server
docker stop nexus-server

# 2. Backup current (potentially corrupted) CAS
mv nexus-data/cas nexus-data/cas.corrupted

# 3. Restore from backup
./scripts/restore.sh --cas backups/daily/nexus-cas-latest.tar.gz

# 4. Merge any recent files from corrupted directory
# (Only if corruption is partial)
rsync -av --ignore-existing nexus-data/cas.corrupted/ nexus-data/cas/

# 5. Restart server
docker start nexus-server

# 6. Verify
curl http://localhost:2026/health
```

### Verification

```bash
# Check file count matches
find nexus-data/cas -type f | wc -l

# Test file retrieval through API
curl -H "Authorization: Bearer $NEXUS_API_KEY" \
  http://localhost:2026/rpc/read?path=/workspace/test.txt
```

---

## Scenario 3: Full Server Loss

### Symptoms
- VM/server unreachable
- All services down
- DNS/IP not responding

### Prerequisites
- Access to a new/replacement server
- GCS/S3 access for backups
- DNS control (if using domain)

### Recovery Steps

#### 1. Provision New Server

```bash
# GCP example
gcloud compute instances create nexus-server-new \
  --zone=us-west1-a \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud
```

#### 2. Install Dependencies

```bash
# SSH to new server
gcloud compute ssh nexus-server-new --zone=us-west1-a

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install gsutil
sudo apt-get install -y google-cloud-sdk
```

#### 3. Clone Repository

```bash
cd ~
git clone https://github.com/nexi-lab/nexus.git
cd nexus
```

#### 4. Download Backups

```bash
# Create backup directory
mkdir -p backups

# Download from GCS
gsutil -m rsync -r gs://my-nexus-backups/backups/ ./backups/

# Verify
ls -la backups/daily/
```

#### 5. Restore Data

```bash
# Start PostgreSQL only
docker compose -f docker-compose.demo.yml up -d postgres

# Wait for PostgreSQL
sleep 30

# Restore database
./scripts/restore.sh --pg backups/daily/nexus-pg-latest.dump

# Restore CAS
./scripts/restore.sh --cas backups/daily/nexus-cas-latest.tar.gz
```

#### 6. Configure Environment

```bash
# Copy environment file
cp .env.example .env

# Edit with production values
vim .env

# Key settings:
# - NEXUS_API_KEY
# - POSTGRES_PASSWORD
# - GCS credentials
```

#### 7. Start All Services

```bash
docker compose -f docker-compose.demo.yml up -d
```

#### 8. Update DNS/Load Balancer

```bash
# Update static IP or DNS record
gcloud compute addresses create nexus-ip --addresses=<NEW_IP>

# Or update DNS A record
```

### Verification

```bash
# Check all services
docker compose -f docker-compose.demo.yml ps

# Test health endpoint
curl http://<NEW_IP>:2026/health

# Test API
curl -H "Authorization: Bearer $NEXUS_API_KEY" \
  http://<NEW_IP>:2026/rpc/list?path=/workspace
```

---

## Scenario 4: Accidental Data Deletion

### Symptoms
- User reports missing files
- Audit log shows delete operations
- Files missing from expected paths

### Diagnosis

```bash
# Check audit log for deletes
docker exec nexus-postgres psql -U postgres -d nexus -c "
SELECT * FROM operation_log
WHERE operation = 'delete'
ORDER BY timestamp DESC
LIMIT 20;
"

# Check if versions exist
docker exec nexus-postgres psql -U postgres -d nexus -c "
SELECT * FROM version_history
WHERE path LIKE '%<filename>%'
ORDER BY created_at DESC;
"
```

### Recovery Steps

#### Option A: Restore from Version History

```bash
# List versions through API
curl -H "Authorization: Bearer $NEXUS_API_KEY" \
  "http://localhost:2026/rpc/versions?path=/workspace/deleted-file.txt"

# Restore specific version
curl -X POST -H "Authorization: Bearer $NEXUS_API_KEY" \
  "http://localhost:2026/rpc/restore?path=/workspace/deleted-file.txt&version=<version_id>"
```

#### Option B: Restore from Backup

```bash
# If deletion was recent and not in version history
# Restore to a test database
./scripts/restore.sh --test backups/daily/nexus-pg-latest.dump

# Query test database for file paths
docker exec nexus-postgres psql -U postgres -d nexus_restore_test -c "
SELECT * FROM file_paths WHERE path LIKE '%<filename>%';
"

# Extract and restore specific files manually
```

---

## Scenario 5: Security Incident / Ransomware

### Immediate Actions

```bash
# 1. IMMEDIATELY isolate the server
gcloud compute firewall-rules create block-all \
  --action=DENY --rules=all --target-tags=nexus-server

# 2. Take VM snapshot for forensics
gcloud compute disks snapshot nexus-disk \
  --snapshot-names=incident-$(date +%Y%m%d%H%M%S)

# 3. Notify security team
# [Contact security team - DO NOT proceed without guidance]
```

### Recovery (After Security Clearance)

```bash
# 1. Provision completely NEW infrastructure
# Do NOT reuse compromised systems

# 2. Restore from VERIFIED clean backup
# Check backup timestamp is before incident

# 3. Rotate ALL credentials
#    - Database passwords
#    - API keys
#    - OAuth tokens
#    - GCS service account keys

# 4. Apply security patches

# 5. Enable additional monitoring
```

---

## Post-Incident Checklist

After any incident:

- [ ] **Timeline documented** - Record all actions taken with timestamps
- [ ] **Root cause identified** - Why did this happen?
- [ ] **Impact assessed** - Data loss, downtime duration, affected users
- [ ] **Backup verified** - Confirm backups are working for next time
- [ ] **Runbook updated** - Add any new learnings to this document
- [ ] **Monitoring improved** - Add alerts to catch this earlier
- [ ] **DR drill scheduled** - Schedule test recovery within 30 days
- [ ] **Stakeholders notified** - Communicate impact and resolution

## Post-Incident Report Template

```markdown
## Incident Report: [Title]

**Date:** YYYY-MM-DD
**Duration:** X hours Y minutes
**Severity:** P1/P2/P3
**Lead:** [Name]

### Summary
[Brief description of what happened]

### Timeline
- HH:MM - Incident detected
- HH:MM - Response initiated
- HH:MM - Root cause identified
- HH:MM - Recovery started
- HH:MM - Service restored
- HH:MM - Post-incident review

### Impact
- Data loss: [Amount/None]
- Downtime: [Duration]
- Affected users: [Count/Description]

### Root Cause
[Detailed explanation]

### Resolution
[Steps taken to resolve]

### Prevention
[Changes to prevent recurrence]

### Action Items
- [ ] Item 1 - Owner - Due date
- [ ] Item 2 - Owner - Due date
```

---

## Appendix: Backup Locations

| Backup Type | Location | Retention |
|-------------|----------|-----------|
| Daily PostgreSQL | `backups/daily/nexus-pg-*.sql.gz` | 30 days |
| Weekly PostgreSQL | `backups/weekly/nexus-pg-*.sql.gz` | 4 weeks |
| Monthly PostgreSQL | `backups/monthly/nexus-pg-*.sql.gz` | 3 months |
| Daily CAS | `backups/daily/nexus-cas-*.tar.gz` | 30 days |
| WAL Archive | `backups/wal/` | Continuous |
| GCS Backups | `gs://<bucket>/backups/` | As configured |

## Appendix: Key File Paths

| Component | Path |
|-----------|------|
| Docker Compose | `docker-compose.demo.yml` |
| Backup script | `scripts/backup.sh` |
| Restore script | `scripts/restore.sh` |
| PostgreSQL data | Docker volume: `nexus_postgres-data` |
| WAL archive | Docker volume: `nexus_postgres-wal-archive` |
| CAS storage | `nexus-data/cas/` |
| Environment | `.env`, `.env.production` |
| Logs | `docker logs <container>` |

## Related Documentation

- [Backup and Recovery Guide](./backup-recovery.md)
- [PostgreSQL Configuration](../deployment/postgresql.md)
- [Server Setup](../deployment/server-setup.md)
- [Administration Operations](../learning-paths/administration-operations.md)
