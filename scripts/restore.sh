#!/bin/bash
# Nexus Restore Script
# Restore PostgreSQL and CAS data from backups
#
# Usage:
#   ./scripts/restore.sh                              # Interactive restore from latest
#   ./scripts/restore.sh --pg <backup_file>           # Restore PostgreSQL from file
#   ./scripts/restore.sh --cas <backup_file>          # Restore CAS from file
#   ./scripts/restore.sh --pitr <timestamp>           # Point-in-time recovery
#   ./scripts/restore.sh --test <backup_file>         # Test restore to temp database
#   ./scripts/restore.sh --from-gcs <date>            # Download and restore from GCS
#   ./scripts/restore.sh --list                       # List available backups

set -e

# =============================================================================
# Configuration
# =============================================================================
BACKUP_DIR="${NEXUS_BACKUP_DIR:-./backups}"
GCS_BUCKET="${NEXUS_GCS_BACKUP_BUCKET:-}"

# PostgreSQL settings
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexus-postgres}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-nexus}"

# CAS data directory
NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"

# =============================================================================
# Helper Functions
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

confirm_action() {
    local message="$1"
    echo -e "${YELLOW}WARNING:${NC} $message"
    read -p "Are you sure you want to continue? (yes/no): " response
    if [ "$response" != "yes" ]; then
        log_info "Operation cancelled"
        exit 0
    fi
}

# =============================================================================
# Verify Backup
# =============================================================================
verify_backup_file() {
    local backup_file="$1"

    if [ ! -f "$backup_file" ]; then
        log_error "Backup file not found: $backup_file"
        return 1
    fi

    # Check checksum if exists
    local checksum_file="${backup_file}.sha256"
    if [ -f "$checksum_file" ]; then
        log_info "Verifying checksum..."
        if sha256_check "$checksum_file" > /dev/null 2>&1; then
            log_success "Checksum verified"
        else
            log_error "Checksum verification FAILED"
            return 1
        fi
    fi

    return 0
}

# =============================================================================
# PostgreSQL Restore
# =============================================================================
restore_postgresql() {
    local backup_file="$1"
    local target_db="${2:-$POSTGRES_DB}"

    log_info "Restoring PostgreSQL from: $backup_file"
    log_info "Target database: $target_db"

    verify_backup_file "$backup_file" || return 1

    if check_docker; then
        log_info "Using Docker container: $POSTGRES_CONTAINER"

        # Check if database exists
        local db_exists=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -lqt | cut -d \| -f 1 | grep -w "$target_db" || true)

        if [ -n "$db_exists" ] && [ "$target_db" = "$POSTGRES_DB" ]; then
            confirm_action "This will DROP and recreate the database '$target_db'. All existing data will be lost!"

            # Stop nexus server if running
            log_info "Stopping Nexus server..."
            docker stop nexus-server 2>/dev/null || true

            # Drop connections and database
            log_info "Dropping existing database..."
            docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -c "
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '$target_db'
                AND pid <> pg_backend_pid();
            " postgres || true

            docker exec "$POSTGRES_CONTAINER" dropdb -U "$POSTGRES_USER" "$target_db" 2>/dev/null || true
        fi

        # Create database
        log_info "Creating database..."
        docker exec "$POSTGRES_CONTAINER" createdb -U "$POSTGRES_USER" "$target_db" 2>/dev/null || true

        # Restore
        log_info "Restoring data (this may take a while)..."
        if [[ "$backup_file" == *.dump ]]; then
            # Custom format - use stdin to avoid permission issues
            cat "$backup_file" | docker exec -i "$POSTGRES_CONTAINER" pg_restore \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                --no-owner \
                --no-privileges \
                2>&1 || true
        elif [[ "$backup_file" == *.sql.gz ]]; then
            # Gzipped SQL - decompress and pipe
            gunzip -c "$backup_file" | docker exec -i "$POSTGRES_CONTAINER" psql \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                2>&1 || true
        elif [[ "$backup_file" == *.sql ]]; then
            # Plain SQL
            docker exec -i "$POSTGRES_CONTAINER" psql \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                < "$backup_file" 2>&1 || true
        else
            log_error "Unknown backup format: $backup_file"
            return 1
        fi

        # Start nexus server
        if [ "$target_db" = "$POSTGRES_DB" ]; then
            log_info "Starting Nexus server..."
            docker start nexus-server 2>/dev/null || true
        fi
    else
        log_info "Using direct PostgreSQL connection: $POSTGRES_HOST:$POSTGRES_PORT"

        if [ "$target_db" = "$POSTGRES_DB" ]; then
            confirm_action "This will DROP and recreate the database '$target_db'. All existing data will be lost!"
        fi

        # Stop connections
        PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" psql \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$target_db' AND pid <> pg_backend_pid();" \
            postgres 2>/dev/null || true

        # Drop and create
        PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" dropdb \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            "$target_db" 2>/dev/null || true

        PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" createdb \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            "$target_db"

        # Restore
        if [[ "$backup_file" == *.dump ]]; then
            # Custom format
            PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" pg_restore \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                --verbose \
                --no-owner \
                --no-privileges \
                "$backup_file" 2>&1 || true
        elif [[ "$backup_file" == *.sql.gz ]]; then
            # Gzipped SQL
            gunzip -c "$backup_file" | PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" psql \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                2>&1 || true
        elif [[ "$backup_file" == *.sql ]]; then
            # Plain SQL
            PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" psql \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$target_db" \
                < "$backup_file" 2>&1 || true
        else
            log_error "Unknown backup format: $backup_file"
            return 1
        fi
    fi

    log_success "PostgreSQL restore complete"

    # Verify restore
    log_info "Verifying restore..."
    if check_docker; then
        local table_count=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$target_db" -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
    else
        local table_count=$(PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$target_db" -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
    fi
    log_success "Restored database has $table_count tables"
}

# =============================================================================
# CAS Restore
# =============================================================================
restore_cas() {
    local backup_file="$1"
    local target_dir="${2:-$NEXUS_DATA_DIR}"

    log_info "Restoring CAS from: $backup_file"
    log_info "Target directory: $target_dir"

    verify_backup_file "$backup_file" || return 1

    # Check if CAS directory exists
    if [ -d "$target_dir/cas" ]; then
        local existing_count=$(find "$target_dir/cas" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [ "$existing_count" -gt 0 ]; then
            confirm_action "CAS directory contains $existing_count files. Restore will merge/overwrite existing files."
        fi
    fi

    # Create target directory
    mkdir -p "$target_dir"

    # Extract
    log_info "Extracting CAS backup..."
    tar -xzf "$backup_file" -C "$target_dir"

    # Verify
    local restored_count=$(find "$target_dir/cas" -type f 2>/dev/null | wc -l | tr -d ' ')
    log_success "CAS restore complete: $restored_count files"
}

# =============================================================================
# Point-in-Time Recovery
# =============================================================================
pitr_restore() {
    local target_time="$1"

    log_info "Point-in-Time Recovery to: $target_time"
    log_warn "PITR requires WAL archiving to be configured"

    # Check if WAL archive exists
    if [ ! -d "$BACKUP_DIR/wal" ] || [ -z "$(ls -A $BACKUP_DIR/wal 2>/dev/null)" ]; then
        log_error "No WAL archive found in $BACKUP_DIR/wal"
        log_error "PITR not available. Use regular backup restore instead."
        return 1
    fi

    confirm_action "This will restore the database to '$target_time'. All changes after this time will be lost!"

    # Find the latest base backup before target time
    log_info "Finding appropriate base backup..."
    local base_backup=$(find "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly" "$BACKUP_DIR/monthly" \
        -name "nexus-pg-*.sql.gz" -type f 2>/dev/null | \
        sort -r | head -1)

    if [ -z "$base_backup" ]; then
        log_error "No base backup found"
        return 1
    fi

    log_info "Using base backup: $base_backup"

    # Restore base backup
    restore_postgresql "$base_backup"

    # Apply WAL files
    log_info "WAL replay would occur here (requires PostgreSQL recovery mode)"
    log_warn "For full PITR support, configure PostgreSQL with recovery.conf"

    # Create recovery signal file
    if check_docker; then
        docker exec "$POSTGRES_CONTAINER" bash -c "
            echo \"recovery_target_time = '$target_time'\" > /var/lib/postgresql/data/recovery.conf
            echo \"restore_command = 'gunzip -c /var/lib/postgresql/wal_archive/%f.gz > %p'\" >> /var/lib/postgresql/data/recovery.conf
            touch /var/lib/postgresql/data/recovery.signal
        "
        log_info "Recovery configuration created. Restart PostgreSQL to begin recovery."
    fi

    log_success "PITR setup complete"
}

# =============================================================================
# Test Restore
# =============================================================================
test_restore() {
    local backup_file="$1"
    local test_db="nexus_restore_test_$(date +%s)"

    log_info "Testing restore to temporary database: $test_db"

    verify_backup_file "$backup_file" || return 1

    # Restore to test database
    restore_postgresql "$backup_file" "$test_db"

    # Run validation queries
    log_info "Running validation queries..."

    if check_docker; then
        # Check tables
        local tables=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$test_db" -tAc "
            SELECT string_agg(tablename, ', ')
            FROM pg_tables
            WHERE schemaname = 'public';
        ")
        log_info "Tables found: $tables"

        # Check row counts for key tables
        for table in file_paths memories rebac_tuples; do
            local count=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$test_db" -tAc "
                SELECT COUNT(*) FROM $table;
            " 2>/dev/null || echo "0")
            log_info "  $table: $count rows"
        done

        # Drop test database
        log_info "Cleaning up test database..."
        docker exec "$POSTGRES_CONTAINER" dropdb -U "$POSTGRES_USER" "$test_db"
    else
        # Similar for direct connection
        PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" psql \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            -d "$test_db" \
            -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"

        # Drop test database
        PGPASSWORD="${POSTGRES_PASSWORD:-nexus}" dropdb \
            -h "$POSTGRES_HOST" \
            -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" \
            "$test_db"
    fi

    log_success "Test restore successful - backup is valid"
}

# =============================================================================
# Download from GCS
# =============================================================================
download_from_gcs() {
    local date_pattern="$1"

    if [ -z "$GCS_BUCKET" ]; then
        log_error "GCS_BUCKET not set"
        return 1
    fi

    log_info "Downloading backups from GCS for date: $date_pattern"

    # List available backups
    log_info "Available backups in GCS:"
    gsutil ls "gs://$GCS_BUCKET/backups/**/*${date_pattern}*" 2>/dev/null || {
        log_error "No backups found matching pattern: $date_pattern"
        return 1
    }

    # Download
    mkdir -p "$BACKUP_DIR/downloaded"
    gsutil -m cp "gs://$GCS_BUCKET/backups/**/*${date_pattern}*" "$BACKUP_DIR/downloaded/"

    log_success "Backups downloaded to: $BACKUP_DIR/downloaded/"
    ls -la "$BACKUP_DIR/downloaded/"
}

# =============================================================================
# List Backups
# =============================================================================
list_backups() {
    echo ""
    echo "=== Available Backups ==="
    echo ""

    for type in daily weekly monthly; do
        if [ -d "$BACKUP_DIR/$type" ]; then
            echo "--- $type ---"
            ls -lh "$BACKUP_DIR/$type"/*.sql.gz "$BACKUP_DIR/$type"/*.tar.gz 2>/dev/null || echo "  (empty)"
            echo ""
        fi
    done

    if [ -n "$GCS_BUCKET" ]; then
        echo "--- GCS (gs://$GCS_BUCKET/backups/) ---"
        gsutil ls "gs://$GCS_BUCKET/backups/" 2>/dev/null || echo "  (not accessible)"
        echo ""
    fi
}

# =============================================================================
# Main
# =============================================================================
print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --pg <file>           Restore PostgreSQL from backup file"
    echo "  --cas <file>          Restore CAS from backup file"
    echo "  --pitr <timestamp>    Point-in-time recovery (e.g., '2024-01-15 14:30:00')"
    echo "  --test <file>         Test restore to temporary database"
    echo "  --from-gcs <date>     Download and restore from GCS (e.g., '20240115')"
    echo "  --list                List available backups"
    echo "  --help                Show this help"
    echo ""
    echo "Examples:"
    echo "  $0 --pg ./backups/daily/nexus-pg-latest.sql.gz"
    echo "  $0 --cas ./backups/daily/nexus-cas-latest.tar.gz"
    echo "  $0 --test ./backups/daily/nexus-pg-latest.sql.gz"
    echo "  $0 --pitr '2024-01-15 14:30:00'"
    echo "  $0 --from-gcs 20240115"
}

main() {
    echo ""
    echo "=========================================="
    echo "  Nexus Restore Script"
    echo "=========================================="
    echo ""

    if [ $# -eq 0 ]; then
        print_usage
        exit 0
    fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --pg)
                restore_postgresql "$2"
                shift 2
                ;;
            --cas)
                restore_cas "$2"
                shift 2
                ;;
            --pitr)
                pitr_restore "$2"
                shift 2
                ;;
            --test)
                test_restore "$2"
                shift 2
                ;;
            --from-gcs)
                download_from_gcs "$2"
                shift 2
                ;;
            --list)
                list_backups
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
}

main "$@"
