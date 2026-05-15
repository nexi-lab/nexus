# Task Manager Tutorial

The Task Manager provides mission and task lifecycle management backed by NexusFS.
Missions contain tasks, tasks have a state machine, and **lifecycle hooks automatically
drive tasks through the full workflow** — no manual status transitions needed.

## Prerequisites

- Nexus server running locally

```bash
nexusd --host 127.0.0.1 --port 2026
```

## Run the demo

```bash
./examples/tutorials/task-manager/task_manager_demo.sh
```

The script demonstrates:

**Part 1 — Automatic lifecycle:**
1. Create a mission and a task
2. Watch the hooks auto-drive: `created -> running -> in_review -> completed`
3. Worker and copilot comments appear automatically
4. Full audit trail is recorded

**Part 2 — Dependency chain:**
5. Create tasks with `blocked_by` dependencies
6. Watch blocked tasks auto-dispatch when their blockers complete
7. Mission auto-completes when all tasks finish

**Part 3 — Manual API usage:**
8. Add your own comments, audit entries, and artifacts alongside the hooks

## Lifecycle Hooks

Two hooks automate the task lifecycle via VFS write interception:

### Hook 1: Worker (on task_created)

When a task is created (and not blocked), the worker hook fires:

```
created -> running -> [sleep 5s] -> worker comment -> in_review
```

The worker:
1. Transitions the task to `running`
2. Simulates work (5 second delay)
3. Posts a comment: "Work is done for: {instruction}"
4. Transitions to `in_review`

### Hook 2: Copilot Review (on in_review)

When a task reaches `in_review`, the copilot hook fires:

```
in_review -> copilot comment -> [sleep 5s] -> completed
```

The copilot:
1. Posts a comment: "Work is reviewed and approved."
2. Simulates review (5 second delay)
3. Transitions to `completed`

### Dependency Resolution

When a task completes, the system checks for blocked tasks whose dependencies
are now satisfied. Unblocked tasks are automatically dispatched to the worker hook.

## Concepts

### Missions

A mission is a container for related tasks. Missions have a status:
`running` | `partial_complete` | `completed` | `cancelled`

When all tasks in a mission reach a terminal status (completed/failed/cancelled),
the mission is automatically completed.

### Tasks

Tasks belong to a mission and follow a state machine:

```
created -> running -> in_review -> completed
                   -> completed
                   -> failed
                   -> cancelled
```

Tasks can declare dependencies via `blocked_by` — a list of task IDs that must
complete before the task becomes dispatchable.

### Comments

Comments are messages attached to a task, authored by either `copilot` or `worker`.
They track progress, review feedback, and decisions.

### Artifacts

Artifacts are typed references to external resources (documents, code, data, etc.)
that can be attached to tasks as inputs (`input_refs`) or outputs (`output_refs`),
or referenced in comments (`artifact_refs`).

Valid types: `document`, `code`, `folder`, `pr`, `image`, `data`, `spreadsheet`,
`presentation`, `other`.

### Audit Trail

Every significant action is recorded as an audit entry. The unified history
endpoint merges audit entries and comments into a single timeline.

## REST API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v2/missions` | Create mission |
| GET | `/api/v2/missions` | List missions |
| GET | `/api/v2/missions/{id}` | Mission detail + tasks |
| PATCH | `/api/v2/missions/{id}` | Update mission |
| POST | `/api/v2/tasks` | Create task |
| GET | `/api/v2/tasks` | List dispatchable tasks |
| GET | `/api/v2/tasks/{id}` | Task detail + comments + artifacts |
| PATCH | `/api/v2/tasks/{id}` | Update task status |
| POST | `/api/v2/tasks/{id}/audit` | Create audit entry |
| GET | `/api/v2/tasks/{id}/history` | Unified timeline |
| POST | `/api/v2/comments` | Create comment |
| GET | `/api/v2/comments?task_id=` | List comments |
| POST | `/api/v2/artifacts` | Create artifact |
| GET | `/api/v2/tasks/events` | SSE stream |

## Dashboard

A web dashboard is available at:

```
http://localhost:2026/dashboard/tasks
```
