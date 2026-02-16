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

-- HRRN SQL function for ORDER BY at dequeue time
CREATE OR REPLACE FUNCTION hrrn_score(
  enqueued_at TIMESTAMPTZ, est_svc REAL, now_ts TIMESTAMPTZ DEFAULT now()
) RETURNS REAL AS $$
BEGIN
  RETURN (EXTRACT(EPOCH FROM (now_ts - enqueued_at)) + est_svc)
         / GREATEST(est_svc, 0.001);
END; $$ LANGUAGE plpgsql IMMUTABLE;

-- Unified priority function (replaces Python duplicate)
CREATE OR REPLACE FUNCTION compute_effective_tier(
  base_tier SMALLINT, boost_tiers SMALLINT, enqueued_at TIMESTAMPTZ,
  aging_secs INT DEFAULT 120, max_wait INT DEFAULT 600,
  now_ts TIMESTAMPTZ DEFAULT now()
) RETURNS SMALLINT AS $$
DECLARE
  wait_secs REAL;
  aging_boost INT;
  effective INT;
BEGIN
  wait_secs := EXTRACT(EPOCH FROM (now_ts - enqueued_at));
  aging_boost := FLOOR(wait_secs / aging_secs)::INT;
  effective := base_tier - boost_tiers - aging_boost;
  IF wait_secs > max_wait THEN
    effective := LEAST(effective, 1);  -- Escalate to HIGH
  END IF;
  RETURN GREATEST(0, effective)::SMALLINT;
END; $$ LANGUAGE plpgsql IMMUTABLE;

-- Indexes for Astraea dequeue pattern
CREATE INDEX IF NOT EXISTS idx_sched_astraea_dequeue
  ON scheduled_tasks (priority_class, enqueued_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_sched_executor_state
  ON scheduled_tasks (executor_state) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_sched_agent_running
  ON scheduled_tasks (agent_id) WHERE status = 'running';
