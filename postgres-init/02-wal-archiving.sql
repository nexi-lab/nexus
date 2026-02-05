-- WAL Archiving Verification Script
-- This script verifies that WAL archiving is properly configured for point-in-time recovery.

-- Check WAL archiving status
DO $$
DECLARE
    archive_mode_val text;
    archive_command_val text;
    wal_level_val text;
BEGIN
    SELECT setting INTO archive_mode_val FROM pg_settings WHERE name = 'archive_mode';
    SELECT setting INTO archive_command_val FROM pg_settings WHERE name = 'archive_command';
    SELECT setting INTO wal_level_val FROM pg_settings WHERE name = 'wal_level';

    RAISE NOTICE '=== WAL Archiving Configuration ===';
    RAISE NOTICE 'archive_mode: %', archive_mode_val;
    RAISE NOTICE 'archive_command: %', COALESCE(archive_command_val, '(not set)');
    RAISE NOTICE 'wal_level: %', wal_level_val;

    IF archive_mode_val = 'off' THEN
        RAISE WARNING 'WAL archive_mode is OFF. Point-in-time recovery (PITR) is NOT available.';
    ELSIF archive_command_val IS NULL OR archive_command_val = '' THEN
        RAISE WARNING 'WAL archive_mode is ON but archive_command is not set.';
    ELSE
        RAISE NOTICE 'WAL archiving is properly configured for PITR.';
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

COMMENT ON FUNCTION nexus_check_wal_archive_status() IS 'Check WAL archiving configuration status for Nexus backup/recovery';
