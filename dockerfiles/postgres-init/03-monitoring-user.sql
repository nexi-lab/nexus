-- Create a dedicated monitoring user for postgres_exporter (Issue #762)
-- Grants read-only access to system statistics views.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_monitor') THEN
        CREATE ROLE nexus_monitor LOGIN PASSWORD 'nexus_monitor';
    END IF;
END
$$;

GRANT pg_monitor TO nexus_monitor;
GRANT SELECT ON pg_stat_statements TO nexus_monitor;
