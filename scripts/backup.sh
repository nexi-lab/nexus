#!/bin/bash
# Nexus Backup Script
# Automated backup for PostgreSQL and CAS data with retention management
#
# Usage:
#   ./scripts/backup.sh                    # Full backup (PostgreSQL + CAS)
#   ./scripts/backup.sh --pg-only          # PostgreSQL only
#   ./scripts/backup.sh --cas-only         # CAS data only
#   ./scripts/backup.sh --gcs              # Upload to GCS after backup
#   ./scripts/backup.sh --verify           # Verify last backup integrity
#   ./scripts/backup.sh --cleanup          # Remove old backups (retention policy)
#   ./scripts/backup.sh --dry-run          # Show what would be done
#   ./scripts/backup.sh --list             # List existing backups

set -e

# =============================================================================
# Configuration (override with environment variables)
# =============================================================================
BACKUP_DIR="${NEXUS_BACKUP_DIR:-./backups}"
GCS_BUCKET="${NEXUS_GCS_BACKUP_BUCKET:-}"
RETENTION_DAYS="${NEXUS_BACKUP_RETENTION_DAYS:-30}"
RETENTION_WEEKLY="${NEXUS_BACKUP_RETENTION_WEEKLY:-4}"
RETENTION_MONTHLY="${NEXUS_BACKUP_RETENTION_MONTHLY:-3}"

# PostgreSQL settings
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexus-postgres}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-nexus}"

# CAS data directory
NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"

# Timestamp for backup files
DATE=$(date +%Y%m%d-%H%M%S)
DAY_OF_WEEK=$(date +%u)  # 1=Monday, 7=Sunday
DAY_OF_MONTH=$(date +%d)

# =============================================================================
# Helper Functions
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Cross-platform sha256 checksum
sha256_sum() {
    if command -v sha256sum &> /dev/null; then
        sha256sum "$@"
    elif command -v shasum &> /dev/null; then
        shasum -a 256 "$@"
    else
        log_warn "No sha256 tool found, skipping checksum"
        return 1
    fi
}

sha256_check() {
    if command -v sha256sum &> /dev/null; then
        sha256sum -c "$@"
    elif command -v shasum &> /dev/null; then
        shasum -a 256 -c "$@"
    else
        log_warn "No sha256 tool found, skipping verification"
        return 1
    fi
}

check_docker() {
    if docker ps --filter "name=$POSTGRES_CONTAINER" --format "{{.Names}}" | grep -q "$POSTGRES_CONTAINER"; then
        return 0
    else
        return 1
    fi
}

ensure_backup_dir() {
    mkdir -p "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR/daily"
    mkdir -p "$BACKUP_DIR/weekly"
    mkdir -p "$BACKUP_DIR/monthly"
    mkdir -p "$BACKUP_DIR/wal"
}

# =============================================================================
# PostgreSQL Backup
# =============================================================================
backup_postgresql() {
    local backup_type="${1:-daily}"
    # Use .dump extension for custom format (includes built-in compression)
    local backup_file="$BACKUP_DIR/$backup_type/nexus-pg-${DATE}.dump"

    log_info "Starting PostgreSQL backup..."

    if check_docker; then
        log_info "Using Docker container: $POSTGRES_CONTAINER"
        if ! docker exec "$POSTGRES_CONTAINER" pg_dump \
            -U "$POSTGRES_USER" \
            -d "$POSTGRES_DB" \
            --format=custom \
            --compress=6 \
            > "$backup_file"; then
            log_error "pg_dump failed"
            return 1
        fi
    else
        log_info "Using direct PostgreSQL connection: $POSTGRES_HOST:$POSTGRES_PORT"
        if ! PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" pg_dump \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            -d "$POSTGRES_DB" \
            --format=custom \
            --compress=6 \
            > "$backup_file"; then
            log_error "pg_dump failed"
            return 1
        fi
    fi

    # Verify backup was created
    if [ -f "$backup_file" ] && [ -s "$backup_file" ]; then
        local size=$(du -h "$backup_file" | cut -f1)
        log_success "PostgreSQL backup created: $backup_file ($size)"

        # Create checksum
        sha256_sum "$backup_file" > "${backup_file}.sha256"
        log_info "Checksum saved: ${backup_file}.sha256"

        # Create latest symlink
        ln -sf "$(basename "$backup_file")" "$BACKUP_DIR/$backup_type/nexus-pg-latest.dump"

        echo "$backup_file"
    else
        log_error "Failed to create PostgreSQL backup"
        rm -f "$backup_file"
        return 1
    fi
}

# =============================================================================
# CAS Backup
# =============================================================================
backup_cas() {
    local backup_type="${1:-daily}"
    local backup_file="$BACKUP_DIR/$backup_type/nexus-cas-${DATE}.tar.gz"
    local cas_dir="$NEXUS_DATA_DIR/cas"

    log_info "Starting CAS backup..."

    if [ ! -d "$cas_dir" ]; then
        log_warn "CAS directory not found: $cas_dir"
        log_warn "Skipping CAS backup"
        return 0
    fi

    # Count files
    local file_count=$(find "$cas_dir" -type f | wc -l | tr -d ' ')
    log_info "Found $file_count files in CAS"

    # Create tarball
    tar -czf "$backup_file" \
        -C "$NEXUS_DATA_DIR" \
        --exclude='*.tmp' \
        cas 2>&1

    if [ -f "$backup_file" ] && [ -s "$backup_file" ]; then
        local size=$(du -h "$backup_file" | cut -f1)
        log_success "CAS backup created: $backup_file ($size)"

        # Create checksum
        sha256_sum "$backup_file" > "${backup_file}.sha256"
        log_info "Checksum saved: ${backup_file}.sha256"

        # Create latest symlink
        ln -sf "$(basename "$backup_file")" "$BACKUP_DIR/$backup_type/nexus-cas-latest.tar.gz"

        echo "$backup_file"
    else
        log_error "Failed to create CAS backup"
        rm -f "$backup_file"
        return 1
    fi
}

# =============================================================================
# WAL Archive Backup
# =============================================================================
backup_wal_archive() {
    local wal_archive_dir="$BACKUP_DIR/wal"

    log_info "Backing up WAL archive..."

    if check_docker; then
        # Check if WAL archive volume exists
        local wal_path=$(docker exec "$POSTGRES_CONTAINER" ls -la /var/lib/postgresql/wal_archive 2>/dev/null || echo "")
        if [ -z "$wal_path" ]; then
            log_warn "WAL archive not configured or empty"
            return 0
        fi

        # Copy WAL files from container
        docker cp "$POSTGRES_CONTAINER:/var/lib/postgresql/wal_archive/." "$wal_archive_dir/" 2>/dev/null || true

        local wal_count=$(find "$wal_archive_dir" -type f -name "*.gz" 2>/dev/null | wc -l | tr -d ' ')
        log_success "WAL archive synced: $wal_count files"
    else
        log_info "Direct PostgreSQL connection - WAL archive should be on filesystem"
    fi
}

# =============================================================================
# GCS Upload
# =============================================================================
upload_to_gcs() {
    if [ -z "$GCS_BUCKET" ]; then
        log_warn "GCS_BUCKET not set, skipping cloud upload"
        return 0
    fi

    log_info "Uploading backups to GCS: gs://$GCS_BUCKET/backups/"

    # Sync backup directory to GCS
    gsutil -m rsync -r "$BACKUP_DIR" "gs://$GCS_BUCKET/backups/"

    log_success "Backups uploaded to GCS"
}

# =============================================================================
# Backup Verification
# =============================================================================
verify_backup() {
    local backup_file="${1:-$BACKUP_DIR/daily/nexus-pg-latest.dump}"

    log_info "Verifying backup integrity: $backup_file"

    if [ ! -f "$backup_file" ]; then
        log_error "Backup file not found: $backup_file"
        return 1
    fi

    # Verify checksum if exists
    local checksum_file="${backup_file}.sha256"
    if [ -f "$checksum_file" ]; then
        if sha256_check "$checksum_file" > /dev/null 2>&1; then
            log_success "Checksum verification passed"
        else
            log_error "Checksum verification FAILED"
            return 1
        fi
    else
        log_warn "No checksum file found, computing..."
        sha256_sum "$backup_file"
    fi

    # For PostgreSQL backups, try to list contents
    if [[ "$backup_file" == *"-pg-"* ]]; then
        log_info "Listing backup contents..."
        if [[ "$backup_file" == *.dump ]]; then
            # Custom format (pg_dump --format=custom)
            local table_count=0
            local verified=false

            # Try local pg_restore first
            if command -v pg_restore &> /dev/null && pg_restore --list "$backup_file" > /dev/null 2>&1; then
                table_count=$(pg_restore --list "$backup_file" 2>/dev/null | grep -c "TABLE DATA" || echo "0")
                verified=true
            # Fall back to Docker container via stdin (avoids permission issues)
            elif check_docker && cat "$backup_file" | docker exec -i "$POSTGRES_CONTAINER" pg_restore --list > /dev/null 2>&1; then
                table_count=$(cat "$backup_file" | docker exec -i "$POSTGRES_CONTAINER" pg_restore --list 2>/dev/null | grep -c "TABLE DATA" || echo "0")
                verified=true
            fi

            if [ "$verified" = true ]; then
                log_success "PostgreSQL backup valid, contains $table_count tables"
            else
                log_error "Failed to validate PostgreSQL backup (custom format)"
                return 1
            fi
        elif [[ "$backup_file" == *.sql.gz ]]; then
            # Plain SQL gzipped
            if gunzip -t "$backup_file" 2>/dev/null; then
                log_success "PostgreSQL backup (gzip) valid"
            else
                log_error "Failed to validate PostgreSQL backup"
                return 1
            fi
        fi
    fi

    # For CAS backups, verify tarball
    if [[ "$backup_file" == *"-cas-"* ]]; then
        log_info "Verifying tarball integrity..."
        if tar -tzf "$backup_file" > /dev/null 2>&1; then
            local file_count=$(tar -tzf "$backup_file" | wc -l | tr -d ' ')
            log_success "CAS backup valid, contains $file_count entries"
        else
            log_error "Failed to validate CAS backup"
            return 1
        fi
    fi

    log_success "Backup verification complete"
}

# =============================================================================
# Cleanup Old Backups
# =============================================================================
cleanup_old_backups() {
    log_info "Cleaning up old backups (retention: $RETENTION_DAYS days, $RETENTION_WEEKLY weekly, $RETENTION_MONTHLY monthly)"

    # Daily backups - keep for RETENTION_DAYS
    local daily_deleted=$(find "$BACKUP_DIR/daily" -type f -mtime +$RETENTION_DAYS -delete -print 2>/dev/null | wc -l | tr -d ' ')
    if [ "$daily_deleted" -gt 0 ]; then
        log_info "Deleted $daily_deleted old daily backups"
    fi

    # Weekly backups - keep RETENTION_WEEKLY
    local weekly_count=$(find "$BACKUP_DIR/weekly" -type f \( -name "*.sql.gz" -o -name "*.dump" \) | wc -l | tr -d ' ')
    if [ "$weekly_count" -gt "$RETENTION_WEEKLY" ]; then
        local to_delete=$((weekly_count - RETENTION_WEEKLY))
        find "$BACKUP_DIR/weekly" -type f \( -name "*.sql.gz" -o -name "*.dump" \) | sort | head -n "$to_delete" | xargs rm -f
        log_info "Deleted $to_delete old weekly backups"
    fi

    # Monthly backups - keep RETENTION_MONTHLY
    local monthly_count=$(find "$BACKUP_DIR/monthly" -type f \( -name "*.sql.gz" -o -name "*.dump" \) | wc -l | tr -d ' ')
    if [ "$monthly_count" -gt "$RETENTION_MONTHLY" ]; then
        local to_delete=$((monthly_count - RETENTION_MONTHLY))
        find "$BACKUP_DIR/monthly" -type f \( -name "*.sql.gz" -o -name "*.dump" \) | sort | head -n "$to_delete" | xargs rm -f
        log_info "Deleted $to_delete old monthly backups"
    fi

    # Clean up orphaned checksum files
    find "$BACKUP_DIR" -name "*.sha256" -type f | while read checksum_file; do
        local backup_file="${checksum_file%.sha256}"
        if [ ! -f "$backup_file" ]; then
            rm -f "$checksum_file"
        fi
    done

    log_success "Cleanup complete"
}

# =============================================================================
# List Backups
# =============================================================================
list_backups() {
    echo ""
    echo "=== Nexus Backups ==="
    echo ""

    for type in daily weekly monthly; do
        if [ -d "$BACKUP_DIR/$type" ]; then
            echo "--- $type ---"
            ls -lh "$BACKUP_DIR/$type"/*.dump "$BACKUP_DIR/$type"/*.tar.gz 2>/dev/null || echo "  (empty)"
            echo ""
        fi
    done

    # Show disk usage
    echo "--- Disk Usage ---"
    du -sh "$BACKUP_DIR"/* 2>/dev/null || echo "No backups found"
    echo ""

    # Show total
    local total=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
    echo "Total backup size: $total"
}

# =============================================================================
# Full Backup
# =============================================================================
full_backup() {
    local upload_gcs="${1:-false}"
    local backup_type="daily"

    # Determine backup type based on day
    if [ "$DAY_OF_MONTH" = "01" ]; then
        backup_type="monthly"
    elif [ "$DAY_OF_WEEK" = "7" ]; then  # Sunday
        backup_type="weekly"
    fi

    log_info "Starting $backup_type backup at $(date)"
    echo ""

    ensure_backup_dir

    # Backup PostgreSQL
    backup_postgresql "$backup_type"
    echo ""

    # Backup CAS
    backup_cas "$backup_type"
    echo ""

    # Backup WAL archive
    backup_wal_archive
    echo ""

    # Verify backups
    verify_backup "$BACKUP_DIR/$backup_type/nexus-pg-latest.dump"
    echo ""

    # Upload to GCS if requested
    if [ "$upload_gcs" = "true" ]; then
        upload_to_gcs
        echo ""
    fi

    log_success "Backup complete at $(date)"
    echo ""

    # Show summary
    list_backups
}

# =============================================================================
# Main
# =============================================================================
print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --pg-only      Backup PostgreSQL only"
    echo "  --cas-only     Backup CAS data only"
    echo "  --gcs          Upload to GCS after backup"
    echo "  --verify       Verify last backup integrity"
    echo "  --cleanup      Remove old backups (retention policy)"
    echo "  --list         List existing backups"
    echo "  --dry-run      Show what would be done"
    echo "  --help         Show this help"
    echo ""
    echo "Environment variables:"
    echo "  NEXUS_BACKUP_DIR              Backup directory (default: ./backups)"
    echo "  NEXUS_GCS_BACKUP_BUCKET       GCS bucket for cloud backups"
    echo "  NEXUS_BACKUP_RETENTION_DAYS   Daily backup retention (default: 30)"
    echo "  POSTGRES_CONTAINER            PostgreSQL container name (default: nexus-postgres)"
    echo "  POSTGRES_HOST                 PostgreSQL host (default: localhost)"
    echo "  POSTGRES_PORT                 PostgreSQL port (default: 5432)"
    echo "  POSTGRES_USER                 PostgreSQL user (default: postgres)"
    echo "  POSTGRES_DB                   PostgreSQL database (default: nexus)"
    echo "  NEXUS_DATA_DIR                CAS data directory (default: ./nexus-data)"
}

main() {
    local pg_only=false
    local cas_only=false
    local upload_gcs=false
    local verify_only=false
    local cleanup_only=false
    local list_only=false
    local dry_run=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --pg-only)
                pg_only=true
                shift
                ;;
            --cas-only)
                cas_only=true
                shift
                ;;
            --gcs)
                upload_gcs=true
                shift
                ;;
            --verify)
                verify_only=true
                shift
                ;;
            --cleanup)
                cleanup_only=true
                shift
                ;;
            --list)
                list_only=true
                shift
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --help|-h)
                print_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                print_usage
                exit 1
                ;;
        esac
    done

    echo ""
    echo "=========================================="
    echo "  Nexus Backup Script"
    echo "=========================================="
    echo ""

    if [ "$dry_run" = true ]; then
        log_info "DRY RUN MODE - No changes will be made"
        echo ""
        echo "Configuration:"
        echo "  Backup directory: $BACKUP_DIR"
        echo "  GCS bucket: ${GCS_BUCKET:-not set}"
        echo "  PostgreSQL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB"
        echo "  CAS directory: $NEXUS_DATA_DIR"
        echo "  Retention: $RETENTION_DAYS days / $RETENTION_WEEKLY weekly / $RETENTION_MONTHLY monthly"
        echo ""
        if check_docker; then
            log_info "Docker container detected: $POSTGRES_CONTAINER"
        else
            log_info "Using direct PostgreSQL connection"
        fi
        exit 0
    fi

    if [ "$list_only" = true ]; then
        list_backups
        exit 0
    fi

    if [ "$verify_only" = true ]; then
        verify_backup
        exit $?
    fi

    if [ "$cleanup_only" = true ]; then
        cleanup_old_backups
        exit $?
    fi

    ensure_backup_dir

    if [ "$pg_only" = true ]; then
        backup_postgresql
        [ "$upload_gcs" = true ] && upload_to_gcs
    elif [ "$cas_only" = true ]; then
        backup_cas
        [ "$upload_gcs" = true ] && upload_to_gcs
    else
        full_backup "$upload_gcs"
    fi
}

main "$@"
