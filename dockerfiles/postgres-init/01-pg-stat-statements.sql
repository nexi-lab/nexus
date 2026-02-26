-- Enable pg_stat_statements extension for query performance monitoring
-- This runs during PostgreSQL initialization

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Grant access to the extension
GRANT pg_read_all_stats TO postgres;
