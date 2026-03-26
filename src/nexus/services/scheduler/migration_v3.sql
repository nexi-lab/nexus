-- Migration v3: Per-executor dequeue indexes (Issue #2748)
--
-- Two composite partial indexes to accelerate per-executor dequeue queries.
-- Each covers one of the two dequeue strategies (classic tier vs HRRN).
-- Partial index (status = 'queued') keeps the index small.

-- Classic effective_tier ordering: dequeue_by_executor
CREATE INDEX IF NOT EXISTS idx_sched_dequeue_executor
    ON scheduled_tasks (executor_id, effective_tier ASC, enqueued_at ASC)
    WHERE status = 'queued';

-- HRRN ordering: dequeue_hrrn_by_executor
CREATE INDEX IF NOT EXISTS idx_sched_dequeue_hrrn_executor
    ON scheduled_tasks (executor_id, priority_class ASC, enqueued_at ASC)
    WHERE status = 'queued'
      AND executor_state IN ('CONNECTED', 'IDLE', 'UNKNOWN');
