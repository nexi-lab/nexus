"""E2E tests for Task Manager API.

Three user-journey tests using httpx.Client against a real server:
1. Full mission lifecycle
2. Multi-round review loop
3. Task dependency chain

Uses the shared nexus_server fixture from conftest.py (SQLite backend).

Run with:
    pytest tests/e2e/server/test_task_manager_e2e.py -v --override-ini="addopts="
"""

import httpx
import pytest

AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}


@pytest.mark.e2e
class TestDashboardEndpoint:
    """Journey 0: Dashboard HTML is served at /dashboard/tasks."""

    def test_dashboard_serves_html(self, test_app: httpx.Client) -> None:
        r = test_app.get("/dashboard/tasks")
        assert r.status_code == 200, r.text
        assert "text/html" in r.headers.get("content-type", "")
        assert "Nexus Task Manager" in r.text
        assert "/api/v2/missions" in r.text


@pytest.mark.e2e
class TestMissionLifecycle:
    """Journey 1: Full mission lifecycle — create, dispatch, complete."""

    def test_full_mission_lifecycle(self, test_app: httpx.Client) -> None:
        # 1. Create mission
        r = test_app.post(
            "/api/v2/missions",
            headers=AUTH_HEADERS,
            json={"title": "Q1 Analysis", "context_summary": "Quarterly review"},
        )
        assert r.status_code == 201, r.text
        mission = r.json()
        mission_id = mission["id"]
        assert mission["status"] == "running"

        # 2. Create input artifact
        r = test_app.post(
            "/api/v2/artifacts",
            headers=AUTH_HEADERS,
            json={
                "type": "data",
                "uri": "/datasets/q1.csv",
                "title": "Q1 Data",
                "mime_type": "text/csv",
                "size_bytes": 1024,
            },
        )
        assert r.status_code == 201, r.text
        artifact = r.json()
        artifact_id = artifact["id"]

        # 3. Create task A with input_refs
        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Analyze Q1 data",
                "input_refs": [artifact_id],
            },
        )
        assert r.status_code == 201, r.text
        task_a = r.json()
        task_a_id = task_a["id"]
        assert task_a["status"] == "created"
        assert task_a["input_refs"] == [artifact_id]

        # 4. Create task B
        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Generate report",
            },
        )
        assert r.status_code == 201, r.text
        task_b = r.json()
        task_b_id = task_b["id"]

        # 5. List dispatchable tasks — both should be dispatchable
        r = test_app.get("/api/v2/tasks", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        dispatchable = r.json()
        dispatchable_ids = {t["id"] for t in dispatchable}
        assert task_a_id in dispatchable_ids
        assert task_b_id in dispatchable_ids

        # 6. Dispatcher picks up task A: created → running
        r = test_app.patch(
            f"/api/v2/tasks/{task_a_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "running"
        assert r.json()["started_at"] is not None

        # 7. Worker reports progress via comment
        r = test_app.post(
            "/api/v2/comments",
            headers=AUTH_HEADERS,
            json={
                "task_id": task_a_id,
                "author": "worker",
                "content": "Processing data, 50% done",
            },
        )
        assert r.status_code == 201, r.text

        # 8. Complete task A with output artifact
        r = test_app.post(
            "/api/v2/artifacts",
            headers=AUTH_HEADERS,
            json={
                "type": "document",
                "uri": "/reports/q1_analysis.pdf",
                "title": "Q1 Analysis Report",
            },
        )
        assert r.status_code == 201, r.text
        output_artifact_id = r.json()["id"]

        r = test_app.patch(
            f"/api/v2/tasks/{task_a_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed", "output_refs": [output_artifact_id]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        assert r.json()["completed_at"] is not None

        # 9. Complete task B: running → completed
        r = test_app.patch(
            f"/api/v2/tasks/{task_b_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        r = test_app.patch(
            f"/api/v2/tasks/{task_b_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text

        # 10. Complete mission
        r = test_app.patch(
            f"/api/v2/missions/{mission_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed", "conclusion": "Q1 looks great"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        assert r.json()["conclusion"] == "Q1 looks great"

        # 11. Verify mission detail shows both tasks completed
        r = test_app.get(f"/api/v2/missions/{mission_id}", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["status"] == "completed"
        assert len(detail["tasks"]) == 2
        assert all(t["status"] == "completed" for t in detail["tasks"])

        # 12. Verify mission appears in list
        r = test_app.get("/api/v2/missions", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        mission_list = r.json()
        assert any(m["id"] == mission_id for m in mission_list["items"])


@pytest.mark.e2e
class TestReviewLoop:
    """Journey 2: Multi-round review loop — worker submits, copilot reviews."""

    def test_multi_round_review(self, test_app: httpx.Client) -> None:
        # Create mission + task
        r = test_app.post(
            "/api/v2/missions",
            headers=AUTH_HEADERS,
            json={"title": "Review Loop Test"},
        )
        assert r.status_code == 201, r.text
        mission_id = r.json()["id"]

        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Write draft report",
            },
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]

        # Start task: created → running
        r = test_app.patch(
            f"/api/v2/tasks/{task_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text

        # Worker submits partial result with artifact
        r = test_app.post(
            "/api/v2/artifacts",
            headers=AUTH_HEADERS,
            json={
                "type": "document",
                "uri": "/drafts/v1.md",
                "title": "Draft v1",
            },
        )
        assert r.status_code == 201, r.text
        draft_v1_id = r.json()["id"]

        r = test_app.post(
            "/api/v2/comments",
            headers=AUTH_HEADERS,
            json={
                "task_id": task_id,
                "author": "worker",
                "content": "Draft v1 ready for review",
                "artifact_refs": [draft_v1_id],
            },
        )
        assert r.status_code == 201, r.text

        # Worker moves to in_review
        r = test_app.patch(
            f"/api/v2/tasks/{task_id}",
            headers=AUTH_HEADERS,
            json={"status": "in_review"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "in_review"

        # Copilot gives feedback
        r = test_app.post(
            "/api/v2/comments",
            headers=AUTH_HEADERS,
            json={
                "task_id": task_id,
                "author": "copilot",
                "content": "Needs more detail in section 2",
            },
        )
        assert r.status_code == 201, r.text

        # Copilot sends back: in_review → running
        r = test_app.patch(
            f"/api/v2/tasks/{task_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "running"

        # Worker submits updated result
        r = test_app.post(
            "/api/v2/comments",
            headers=AUTH_HEADERS,
            json={
                "task_id": task_id,
                "author": "worker",
                "content": "Updated draft with more detail",
            },
        )
        assert r.status_code == 201, r.text

        # Worker moves to in_review again
        r = test_app.patch(
            f"/api/v2/tasks/{task_id}",
            headers=AUTH_HEADERS,
            json={"status": "in_review"},
        )
        assert r.status_code == 200, r.text

        # Copilot approves: in_review → completed
        r = test_app.patch(
            f"/api/v2/tasks/{task_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"

        # Verify all 3 comments in order
        r = test_app.get(
            f"/api/v2/comments?task_id={task_id}",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200, r.text
        comments = r.json()
        assert len(comments) == 3
        assert comments[0]["author"] == "worker"
        assert comments[1]["author"] == "copilot"
        assert comments[2]["author"] == "worker"

        # Verify full task detail with comment history
        r = test_app.get(f"/api/v2/tasks/{task_id}", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["status"] == "completed"
        assert len(detail["comments"]) == 3
        # Artifact from first comment should be in artifacts list
        assert any(a["id"] == draft_v1_id for a in detail["artifacts"])


@pytest.mark.e2e
class TestDependencyChain:
    """Journey 3: Task dependency chain — blocked_by enforcement."""

    def test_task_dependency_chain(self, test_app: httpx.Client) -> None:
        # Create mission
        r = test_app.post(
            "/api/v2/missions",
            headers=AUTH_HEADERS,
            json={"title": "Dependency Chain Test"},
        )
        assert r.status_code == 201, r.text
        mission_id = r.json()["id"]

        # Create task A (no blocked_by)
        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Step A",
            },
        )
        assert r.status_code == 201, r.text
        task_a_id = r.json()["id"]

        # Create task B (blocked_by=[A])
        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Step B",
                "blocked_by": [task_a_id],
            },
        )
        assert r.status_code == 201, r.text
        task_b_id = r.json()["id"]

        # Create task C (blocked_by=[B])
        r = test_app.post(
            "/api/v2/tasks",
            headers=AUTH_HEADERS,
            json={
                "mission_id": mission_id,
                "instruction": "Step C",
                "blocked_by": [task_b_id],
            },
        )
        assert r.status_code == 201, r.text
        task_c_id = r.json()["id"]

        # Only A is dispatchable
        r = test_app.get("/api/v2/tasks", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        dispatchable = r.json()
        dispatchable_ids = {t["id"] for t in dispatchable}
        assert task_a_id in dispatchable_ids
        assert task_b_id not in dispatchable_ids
        assert task_c_id not in dispatchable_ids

        # Complete A: created → running → completed
        r = test_app.patch(
            f"/api/v2/tasks/{task_a_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        r = test_app.patch(
            f"/api/v2/tasks/{task_a_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text

        # Now B is dispatchable, C still blocked
        r = test_app.get("/api/v2/tasks", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        dispatchable = r.json()
        dispatchable_ids = {t["id"] for t in dispatchable}
        assert task_b_id in dispatchable_ids
        assert task_c_id not in dispatchable_ids

        # Complete B
        r = test_app.patch(
            f"/api/v2/tasks/{task_b_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        r = test_app.patch(
            f"/api/v2/tasks/{task_b_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text

        # Now C is dispatchable
        r = test_app.get("/api/v2/tasks", headers=AUTH_HEADERS)
        assert r.status_code == 200, r.text
        dispatchable = r.json()
        dispatchable_ids = {t["id"] for t in dispatchable}
        assert task_c_id in dispatchable_ids

        # Complete C
        r = test_app.patch(
            f"/api/v2/tasks/{task_c_id}",
            headers=AUTH_HEADERS,
            json={"status": "running"},
        )
        assert r.status_code == 200, r.text
        r = test_app.patch(
            f"/api/v2/tasks/{task_c_id}",
            headers=AUTH_HEADERS,
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text

        # Verify all 3 tasks completed
        for tid in (task_a_id, task_b_id, task_c_id):
            r = test_app.get(f"/api/v2/tasks/{tid}", headers=AUTH_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "completed"
