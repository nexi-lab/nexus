#!/usr/bin/env bash
#
# Task Manager Demo
#
# Demonstrates automated task lifecycle with VFS hooks:
#   1. Create a task -> hooks auto-drive it through the full lifecycle
#   2. Dependency chains -> blocked tasks auto-dispatch when unblocked
#
# Lifecycle hooks:
#   Hook 1 (task_created):
#     created -> running, sleep 5s, worker comment, -> in_review
#   Hook 2 (task in_review):
#     copilot comment, sleep 5s, -> completed
#
# Prerequisites:
#   Nexus server running:  nexusd --host 127.0.0.1 --port 2026
#
# Usage:
#   ./examples/tutorials/task-manager/task_manager_demo.sh

set -euo pipefail

BASE_URL="${NEXUS_URL:-http://127.0.0.1:2026}"
AUTH_HEADER="Authorization: Bearer ${NEXUS_API_KEY:-}"

# Helper: pretty-print JSON response
api() {
    local method="$1" endpoint="$2"
    shift 2
    local response
    response=$(curl -s -w "\n%{http_code}" -X "$method" "${BASE_URL}${endpoint}" \
        -H "Content-Type: application/json" \
        -H "$AUTH_HEADER" \
        "$@")
    local body code
    body=$(echo "$response" | sed '$d')
    code=$(echo "$response" | tail -1)
    if [ "$code" -ge 400 ]; then
        echo "  ERROR ($code): $body"
        return 1
    fi
    echo "$body"
}

# Extract JSON field (portable, no jq dependency required — but uses jq if available)
json_field() {
    if command -v jq &>/dev/null; then
        echo "$1" | jq -r "$2"
    else
        echo "$1" | python3 -c "import sys,json; print(json.load(sys.stdin)$(echo "$2" | sed "s/\./']['/g; s/^/['/; s/$/']/" ))"
    fi
}

# Wait for a task to reach a target status (polling)
wait_for_status() {
    local task_id="$1" target="$2" timeout="${3:-30}"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        local current
        current=$(json_field "$(api GET "/api/v2/tasks/$task_id")" '.status')
        if [ "$current" = "$target" ]; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    echo "  TIMEOUT: task $task_id did not reach '$target' within ${timeout}s (current: $current)"
    return 1
}

echo "============================================"
echo "  Nexus Task Manager Demo"
echo "============================================"
echo
echo "Server: $BASE_URL"
echo

# ==================================================================
# Part 1: Single task — automatic lifecycle
# ==================================================================
echo "========================================"
echo "  Part 1: Automatic Task Lifecycle"
echo "========================================"
echo

# ------------------------------------------------------------------
# 1. Create a mission
# ------------------------------------------------------------------
echo "--- Step 1: Create a mission ---"
mission=$(api POST /api/v2/missions \
    -d '{"title": "Q1 Data Pipeline", "context_summary": "End-to-end quarterly data analysis"}')
mission_id=$(json_field "$mission" '.id')
echo "  Mission created: $mission_id"
echo "  Status: $(json_field "$mission" '.status')"
echo

# ------------------------------------------------------------------
# 2. Create an input artifact
# ------------------------------------------------------------------
echo "--- Step 2: Create an input artifact ---"
artifact=$(api POST /api/v2/artifacts \
    -d '{"type": "data", "uri": "/datasets/q1_sales.csv", "title": "Q1 Sales Data", "mime_type": "text/csv", "size_bytes": 2048}')
artifact_id=$(json_field "$artifact" '.id')
echo "  Artifact created: $artifact_id (Q1 Sales Data)"
echo

# ------------------------------------------------------------------
# 3. Create a task — hooks take over from here
# ------------------------------------------------------------------
echo "--- Step 3: Create task — hooks auto-drive the lifecycle ---"
task_a=$(api POST /api/v2/tasks \
    -d "{\"mission_id\": \"$mission_id\", \"instruction\": \"Clean and validate Q1 sales CSV\", \"input_refs\": [\"$artifact_id\"], \"label\": \"etl\"}")
task_a_id=$(json_field "$task_a" '.id')
echo "  Task created: $task_a_id"
echo "  Initial status: $(json_field "$task_a" '.status')"
echo
echo "  Hook 1 fires: created -> running (worker starts)"
echo "  Waiting for worker to finish (~5s)..."
echo

# ------------------------------------------------------------------
# 4. Wait for in_review (Hook 1 completes)
# ------------------------------------------------------------------
wait_for_status "$task_a_id" "in_review" 15
detail=$(api GET "/api/v2/tasks/$task_a_id")
echo "  Status: $(json_field "$detail" '.status')"
if command -v jq &>/dev/null; then
    echo "  Worker comment:"
    echo "$detail" | jq -r '.comments[]? | select(.author=="worker") | "    [\(.author)] \(.content)"'
fi
echo
echo "  Hook 2 fires: in_review -> copilot review starts"
echo "  Waiting for copilot to finish (~5s)..."
echo

# ------------------------------------------------------------------
# 5. Wait for completed (Hook 2 completes)
# ------------------------------------------------------------------
wait_for_status "$task_a_id" "completed" 15
detail=$(api GET "/api/v2/tasks/$task_a_id")
echo "  Status: $(json_field "$detail" '.status')"
echo "  Started at:   $(json_field "$detail" '.started_at')"
echo "  Completed at: $(json_field "$detail" '.completed_at')"
if command -v jq &>/dev/null; then
    echo "  Comments:"
    echo "$detail" | jq -r '.comments[]? | "    [\(.author)] \(.content)"'
    echo "  Audit trail:"
    echo "$detail" | jq -r '.history[]? | select(.type=="audit") | "    [\(.actor // "system")] \(.detail)"'
fi
echo

# ==================================================================
# Part 2: Dependency chain — auto-dispatch on unblock
# ==================================================================
echo "========================================"
echo "  Part 2: Dependency Chain"
echo "========================================"
echo

# ------------------------------------------------------------------
# 6. Create Task B — blocked by Task A (already completed)
# ------------------------------------------------------------------
echo "--- Step 6: Create Task B — blocked by completed Task A ---"
task_b=$(api POST /api/v2/tasks \
    -d "{\"mission_id\": \"$mission_id\", \"instruction\": \"Generate summary report from cleaned data\", \"blocked_by\": [\"$task_a_id\"], \"label\": \"reporting\"}")
task_b_id=$(json_field "$task_b" '.id')
echo "  Task B created: $task_b_id"
echo "  Blocked by: $task_a_id (already completed)"
echo "  Since blocker is done, Task B will auto-dispatch..."
echo

# ------------------------------------------------------------------
# 7. Create Task C — blocked by Task B (not yet completed)
# ------------------------------------------------------------------
echo "--- Step 7: Create Task C — blocked by Task B ---"
task_c=$(api POST /api/v2/tasks \
    -d "{\"mission_id\": \"$mission_id\", \"instruction\": \"Email report to stakeholders\", \"blocked_by\": [\"$task_b_id\"], \"label\": \"delivery\"}")
task_c_id=$(json_field "$task_c" '.id')
echo "  Task C created: $task_c_id"
echo "  Blocked by: $task_b_id (still in progress)"
echo

# ------------------------------------------------------------------
# 8. Wait for Task B to complete (~10s)
# ------------------------------------------------------------------
echo "--- Step 8: Waiting for Task B to auto-complete (~10s) ---"
wait_for_status "$task_b_id" "completed" 20
echo "  Task B completed!"
echo

# ------------------------------------------------------------------
# 9. Task C auto-dispatches when B completes
# ------------------------------------------------------------------
echo "--- Step 9: Task C auto-dispatched (unblocked by B) ---"
echo "  Waiting for Task C to auto-complete (~10s)..."
wait_for_status "$task_c_id" "completed" 20
echo "  Task C completed!"
echo

# ------------------------------------------------------------------
# 10. Check mission — should be auto-completed
# ------------------------------------------------------------------
echo "--- Step 10: Mission status ---"
mission_detail=$(api GET "/api/v2/missions/$mission_id")
echo "  Mission: $(json_field "$mission_detail" '.title')"
echo "  Status:  $(json_field "$mission_detail" '.status')"
if command -v jq &>/dev/null; then
    echo "  Tasks:"
    echo "$mission_detail" | jq -r '.tasks[] | "    [\(.status)] \(.instruction)"'
fi
echo

# ==================================================================
# Part 3: Manual workflow (API reference)
# ==================================================================
echo "========================================"
echo "  Part 3: Manual API Usage"
echo "========================================"
echo

# ------------------------------------------------------------------
# 11. Create a task and manually drive it
# ------------------------------------------------------------------
echo "--- Step 11: Manual task lifecycle (override hooks) ---"
manual_mission=$(api POST /api/v2/missions \
    -d '{"title": "Manual Demo", "context_summary": "Showing manual API usage"}')
manual_mid=$(json_field "$manual_mission" '.id')

manual_task=$(api POST /api/v2/tasks \
    -d "{\"mission_id\": \"$manual_mid\", \"instruction\": \"Review pull request #42\"}")
manual_tid=$(json_field "$manual_task" '.id')
echo "  Task created: $manual_tid"

# Note: hooks will also fire, but we can add our own comments/audit
echo "  Adding manual comment..."
api POST /api/v2/comments \
    -d "{\"task_id\": \"$manual_tid\", \"author\": \"worker\", \"content\": \"PR looks good, 2 minor nits.\"}" >/dev/null

echo "  Adding audit entry..."
api POST "/api/v2/tasks/$manual_tid/audit" \
    -d '{"action": "code_review", "actor": "worker", "detail": "Reviewed 3 files, 150 lines changed"}' >/dev/null

echo "  Checking history..."
sleep 1
detail=$(api GET "/api/v2/tasks/$manual_tid")
if command -v jq &>/dev/null; then
    echo "  All comments:"
    echo "$detail" | jq -r '.comments[]? | "    [\(.author)] \(.content)"'
fi
echo

# ------------------------------------------------------------------
# 12. List all missions
# ------------------------------------------------------------------
echo "--- Step 12: List all missions ---"
missions=$(api GET /api/v2/missions)
if command -v jq &>/dev/null; then
    echo "$missions" | jq -r '.items[] | "  [\(.status)] \(.title)"'
fi
echo

echo "============================================"
echo "  Demo complete!"
echo "  Dashboard: ${BASE_URL}/dashboard/tasks"
echo "============================================"
