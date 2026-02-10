-- Scheduler: Hybrid Priority Task Queue
-- Related: Issue #1212
--
-- Run this migration against the PostgreSQL metadata database
-- to create the scheduled_tasks table.

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Submission data
    agent_id TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',

    -- Priority data
    priority_tier SMALLINT NOT NULL DEFAULT 2,  -- PriorityTier.NORMAL
    deadline TIMESTAMPTZ,
    boost_amount NUMERIC(12,6) NOT NULL DEFAULT 0,
    boost_tiers SMALLINT NOT NULL DEFAULT 0,

    -- Computed priority (materialized for O(1) dequeue)
    effective_tier SMALLINT NOT NULL DEFAULT 2,

    -- Timestamps
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- Status: queued | running | completed | failed | cancelled
    status TEXT NOT NULL DEFAULT 'queued',

    -- Payment integration
    boost_reservation_id TEXT,
    idempotency_key TEXT UNIQUE,

    -- Multi-tenancy
    zone_id TEXT NOT NULL DEFAULT 'default',

    -- Error tracking
    error_message TEXT
);

-- Partial index for efficient priority-ordered dequeue (Issue #15)
-- Only indexes active (queued) tasks, stays small as tasks complete
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_dequeue
ON scheduled_tasks (effective_tier, enqueued_at)
WHERE status = 'queued';

-- Index for task status queries by agent
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_agent_status
ON scheduled_tasks (agent_id, status);

-- Index for idempotency key lookups (unique constraint handles this)
-- Already covered by the UNIQUE constraint on idempotency_key
