#!/bin/bash
# Clean up test data from Nexus database
#
# This script removes all test files created by the workflow demo
# Use this if the demo fails due to duplicate key errors

set -e

echo "🧹 Nexus Database Cleanup"
echo "========================"
echo ""

# PostgreSQL connection settings
PG_HOST="${POSTGRES_HOST:-localhost}"
PG_PORT="${POSTGRES_PORT:-5432}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_PASSWORD="${POSTGRES_PASSWORD:-nexus}"
PG_DB="${POSTGRES_DB:-nexus}"

echo "📍 Target Database:"
echo "   Host: $PG_HOST:$PG_PORT"
echo "   Database: $PG_DB"
echo ""

# Confirm before proceeding
read -p "⚠️  This will delete ALL files under /uploads/. Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Cancelled"
    exit 1
fi

echo ""
echo "🗑️  Deleting test files from database..."

# Delete all files under /uploads/
PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" <<SQL
-- Delete file metadata
DELETE FROM file_paths WHERE virtual_path LIKE '/uploads/%';

-- Show remaining count
SELECT COUNT(*) as remaining_files FROM file_paths;
SQL

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "You can now run the demo again:"
echo "  python workflow_auto_fire_demo.py"
