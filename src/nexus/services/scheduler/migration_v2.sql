-- Astraea-style state-aware scheduler migration (Issue #1274)
-- All statements are idempotent (safe to re-run).

-- Add Astraea columns
ALTER TABLE scheduled_tasks
  ADD COLUMN IF NOT EXISTS request_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE scheduled_tasks
  ADD COLUMN IF NOT EXISTS priority_class TEXT NOT NULL DEFAULT 'batch';
ALTER TABLE scheduled_tasks
  ADD COLUMN IF NOT EXISTS executor_state TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE scheduled_tasks
  ADD COLUMN IF NOT EXISTS estimated_service_time REAL NOT NULL DEFAULT 30.0;

-- NOTE: hrrn_score() and compute_effective_tier() PL/pgSQL functions removed.
-- HRRN scoring is now inlined in SQL queries (queue.py).
-- Effective tier computation uses Python (scheduler/priority.py).

-- Drop legacy PL/pgSQL functions if they exist
DROP FUNCTION IF EXISTS hrrn_score(TIMESTAMPTZ, REAL, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS compute_effective_tier(SMALLINT, SMALLINT, TIMESTAMPTZ, INT, INT, TIMESTAMPTZ);

-- Indexes for Astraea dequeue pattern
CREATE INDEX IF NOT EXISTS idx_sched_astraea_dequeue
  ON scheduled_tasks (priority_class, enqueued_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_sched_executor_state
  ON scheduled_tasks (executor_state) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_sched_agent_running
  ON scheduled_tasks (agent_id) WHERE status = 'running';

-- Zone-filtered pending count (Issue #2360: supports count_pending(zone_id=...) in queue.py)
CREATE INDEX IF NOT EXISTS idx_sched_zone_pending
  ON scheduled_tasks (zone_id) WHERE status = 'queued';
