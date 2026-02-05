-- WAL Archiving Verification Script
-- This script verifies that WAL archiving is properly configured for point-in-time recovery.
-- It runs during PostgreSQL initialization and logs warnings if archiving is not enabled.

-- Check WAL archiving status
DO $$
DECLARE
    archive_mode_val text;
    archive_command_val text;
    wal_level_val text;
BEGIN
    -- Get current settings
    SELECT setting INTO archive_mode_val FROM pg_settings WHERE name = 'archive_mode';
    SELECT setting INTO archive_command_val FROM pg_settings WHERE name = 'archive_command';
    SELECT setting INTO wal_level_val FROM pg_settings WHERE name = 'wal_level';

    -- Log current configuration
    RAISE NOTICE '=== WAL Archiving Configuration ===';
    RAISE NOTICE 'archive_mode: %', archive_mode_val;
    RAISE NOTICE 'archive_command: %', COALESCE(archive_command_val, '(not set)');
    RAISE NOTICE 'wal_level: %', wal_level_val;

    -- Check if archiving is properly configured
    IF archive_mode_val = 'off' THEN
        RAISE WARNING 'WAL archive_mode is OFF. Point-in-time recovery (PITR) is NOT available.';
        RAISE WARNING 'To enable PITR, add these PostgreSQL parameters:';
        RAISE WARNING '  -c archive_mode=on';
        RAISE WARNING '  -c archive_command=''cp %%p /var/lib/postgresql/wal_archive/%%f''';
    ELSIF archive_command_val IS NULL OR archive_command_val = '' THEN
        RAISE WARNING 'WAL archive_mode is ON but archive_command is not set.';
        RAISE WARNING 'WAL files will not be archived. PITR may not work correctly.';
    ELSE
        RAISE NOTICE 'WAL archiving is properly configured for PITR.';
    END IF;

    -- Check wal_level for replication support
    IF wal_level_val = 'minimal' THEN
        RAISE WARNING 'wal_level is minimal. Replication and some backup tools may not work.';
        RAISE WARNING 'Consider setting: -c wal_level=replica';
    END IF;
END $$;

-- Create helper function to check archive status
CREATE OR REPLACE FUNCTION nexus_check_wal_archive_status()
RETURNS TABLE (
    setting_name text,
    setting_value text,
    status text
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.name::text,
        s.setting::text,
        CASE
            WHEN s.name = 'archive_mode' AND s.setting = 'on' THEN 'OK'
            WHEN s.name = 'archive_mode' AND s.setting = 'off' THEN 'WARNING: Archiving disabled'
            WHEN s.name = 'archive_command' AND s.setting != '' THEN 'OK'
            WHEN s.name = 'archive_command' AND s.setting = '' THEN 'WARNING: No archive command'
            WHEN s.name = 'wal_level' AND s.setting IN ('replica', 'logical') THEN 'OK'
            WHEN s.name = 'wal_level' AND s.setting = 'minimal' THEN 'WARNING: Minimal WAL level'
            ELSE 'INFO'
        END
    FROM pg_settings s
    WHERE s.name IN ('archive_mode', 'archive_command', 'archive_timeout', 'wal_level', 'wal_compression');
END;
$$ LANGUAGE plpgsql;

-- Show current archive statistics
-- This will be empty if archiving is disabled
SELECT
    archived_count,
    last_archived_wal,
    last_archived_time,
    failed_count,
    last_failed_wal,
    last_failed_time
FROM pg_stat_archiver;

COMMENT ON FUNCTION nexus_check_wal_archive_status() IS 'Check WAL archiving configuration status for Nexus backup/recovery';
