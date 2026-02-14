"""E2E tests: Async skills & share_link endpoints via FastAPI server.

Validates that the async conversion (Issue #1287, Decision 12A) works
correctly end-to-end when dispatched through the FastAPI auto-dispatch
mechanism. Tests cover:

1. Skills CRUD: discover, subscribe, unsubscribe, share, load, prompt context
2. Skills import/export: export, import, validate_zip
3. Share links: create, list, get, revoke, access, access_logs

The FastAPI auto-dispatch detects async methods via
``asyncio.iscoroutinefunction()`` and ``await``s them directly, so these
tests exercise the full async chain:
  HTTP -> FastAPI -> auto_dispatch(await) -> NexusFS.skills_*(async)
       -> SkillService.*(async) -> asyncio.to_thread(sync I/O)

The e2e server runs in open-access mode (no --api-key).  Identity is
established via the ``X-Nexus-Subject`` header so that skill ownership
checks pass (skill paths contain ``/user/admin/``).
"""

from __future__ import annotations

import base64
import io
import time
import zipfile

# Open access mode: identity via X-Nexus-Subject header
HEADERS = {
    "X-Nexus-Subject": "user:admin",
    "X-Nexus-Zone-Id": "default",
}


def _b64(text: str) -> dict:
    """Encode string as RPC bytes value."""
    return {"__type__": "bytes", "data": base64.b64encode(text.encode()).decode()}


def rpc(client, method: str, params: dict | None = None) -> dict:
    """Make a JSON-RPC call and return the parsed response."""
    body = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }
    resp = client.post(f"/api/nfs/{method}", json=body, headers=HEADERS)
    return {"status": resp.status_code, "body": resp.json()}


def rpc_result(client, method: str, params: dict | None = None):
    """Make a JSON-RPC call and return just the result (assert 200 + no error)."""
    data = rpc(client, method, params)
    assert data["status"] == 200, f"Expected 200, got {data['status']}: {data['body']}"
    body = data["body"]
    assert "error" not in body or body.get("error") is None, (
        f"RPC error in {method}: {body.get('error')}"
    )
    return body.get("result")


def write_file(client, path: str, text: str):
    """Write a file via RPC with proper bytes encoding."""
    return rpc_result(client, "write", {"path": path, "content": _b64(text)})


def write_skill(client, skill_dir: str, text: str):
    """Write a SKILL.md and wait for deferred permission buffer to flush.

    The server uses a DeferredPermissionBuffer (50ms flush interval) to batch
    ownership tuple writes.  A short delay after writing ensures the ReBAC
    ownership grant is visible for subsequent permission checks (e.g.,
    ``_can_read_skill``, ``_assert_skill_owner``).
    """
    write_file(client, f"{skill_dir}SKILL.md", text)
    time.sleep(0.2)  # Allow deferred buffer flush (50ms interval × 3+ cycles)


# ─── Skills Endpoints ──────────────────────────────────────────────────


class TestSkillsDiscoverE2E:
    """skills_discover through FastAPI async dispatch."""

    def test_discover_returns_empty_initially(self, test_app):
        """Discover on fresh server returns zero skills."""
        result = rpc_result(test_app, "skills_discover", {"filter": "all"})
        assert result["count"] == 0
        assert result["skills"] == []

    def test_discover_after_write_and_subscribe(self, test_app):
        """Create a skill, subscribe, then discover it."""
        skill_dir = "/zone/default/user/admin/skill/e2e-test/"
        write_skill(
            test_app,
            skill_dir,
            "---\nname: E2E Test Skill\ndescription: Async validation\nauthor: admin\n---\n# E2E Skill\nHello from async.",
        )

        sub = rpc_result(test_app, "skills_subscribe", {"skill_path": skill_dir})
        assert sub["success"] is True

        result = rpc_result(test_app, "skills_discover", {"filter": "subscribed"})
        assert result["count"] >= 1
        names = [s["name"] for s in result["skills"]]
        assert "E2E Test Skill" in names


class TestSkillsSubscribeE2E:
    """skills_subscribe / skills_unsubscribe through FastAPI."""

    def test_subscribe_unsubscribe_cycle(self, test_app):
        """Subscribe then unsubscribe a skill path (must exist first)."""
        path = "/zone/default/user/admin/skill/sub-test/"

        # Write skill first so ownership ReBAC tuple exists
        write_skill(
            test_app,
            path,
            "---\nname: Sub Test\n---\nContent",
        )

        sub = rpc_result(test_app, "skills_subscribe", {"skill_path": path})
        assert sub["success"] is True
        assert sub["already_subscribed"] is False

        sub2 = rpc_result(test_app, "skills_subscribe", {"skill_path": path})
        assert sub2["success"] is True
        assert sub2["already_subscribed"] is True

        unsub = rpc_result(test_app, "skills_unsubscribe", {"skill_path": path})
        assert unsub["success"] is True
        assert unsub["was_subscribed"] is True

        unsub2 = rpc_result(test_app, "skills_unsubscribe", {"skill_path": path})
        assert unsub2["success"] is True
        assert unsub2["was_subscribed"] is False


class TestSkillsLoadE2E:
    """skills_load through FastAPI."""

    def test_load_skill_content(self, test_app):
        """Write a skill then load it."""
        skill_dir = "/zone/default/user/admin/skill/loadable/"
        write_skill(
            test_app,
            skill_dir,
            "---\nname: Loadable Skill\ndescription: Can be loaded\nauthor: admin\nversion: '3.0'\n---\n# Instructions\nDo the thing.",
        )

        loaded = rpc_result(test_app, "skills_load", {"skill_path": skill_dir})
        assert loaded["name"] == "Loadable Skill"
        assert loaded["owner"] == "admin"
        assert "Instructions" in loaded["content"]
        assert loaded["metadata"]["version"] == "3.0"


class TestSkillsPromptContextE2E:
    """skills_get_prompt_context through FastAPI."""

    def test_prompt_context_empty(self, test_app):
        """Prompt context with no subscriptions."""
        result = rpc_result(test_app, "skills_get_prompt_context")
        assert result["count"] == 0
        assert "<available_skills>" in result["xml"]

    def test_prompt_context_with_subscription(self, test_app):
        """Subscribe to a skill then get prompt context."""
        skill_dir = "/zone/default/user/admin/skill/prompt-ctx/"
        write_skill(
            test_app,
            skill_dir,
            "---\nname: PromptCtx Skill\ndescription: For prompt context\n---\nUse this skill.",
        )
        rpc_result(test_app, "skills_subscribe", {"skill_path": skill_dir})

        result = rpc_result(test_app, "skills_get_prompt_context")
        assert result["count"] >= 1
        assert "PromptCtx Skill" in result["xml"]


class TestSkillsShareE2E:
    """skills_share / skills_unshare through FastAPI."""

    def test_share_public_and_unshare(self, test_app):
        """Share a skill publicly then unshare it."""
        skill_dir = "/zone/default/user/admin/skill/shareable/"
        write_skill(
            test_app,
            skill_dir,
            "---\nname: Shareable\n---\nContent",
        )

        shared = rpc_result(
            test_app,
            "skills_share",
            {"skill_path": skill_dir, "share_with": "public"},
        )
        assert shared["success"] is True
        assert shared["share_with"] == "public"

        # unshare uses param name "unshare_from" per SkillsUnshareParams
        unshared = rpc_result(
            test_app,
            "skills_unshare",
            {"skill_path": skill_dir, "unshare_from": "public"},
        )
        assert unshared["success"] is True


# ─── Skills Export / Import / Validate ──────────────────────────────────


def _make_skill_zip(name: str = "Imported", description: str = "A test skill") -> str:
    """Create a minimal .skill ZIP and return base64-encoded data."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        skill_md = f"---\nname: {name}\ndescription: {description}\nauthor: admin\nversion: '1.0'\n---\n# {name}\nInstructions here."
        # ZIP must contain SKILL.md inside a named folder (e.g., name/SKILL.md)
        zf.writestr(f"{name}/SKILL.md", skill_md)
    return base64.b64encode(buf.getvalue()).decode()


class TestSkillsExportE2E:
    """skills_export through FastAPI async dispatch."""

    def test_export_skill(self, test_app):
        """Write a skill, then export it."""
        skill_dir = "/zone/default/user/admin/skill/exportable/"
        write_skill(
            test_app,
            skill_dir,
            "---\nname: Exportable\ndescription: Can export\nauthor: admin\nversion: '2.0'\n---\n# Export\nContent.",
        )

        result = rpc_result(
            test_app,
            "skills_export",
            {"skill_name": "exportable"},
        )
        # Export returns dict with zip_data (base64) or file path
        assert isinstance(result, dict)


class TestSkillsValidateZipE2E:
    """skills_validate_zip through FastAPI async dispatch."""

    def test_validate_valid_zip(self, test_app):
        """Validate a well-formed .skill ZIP."""
        zip_data = _make_skill_zip()
        result = rpc_result(
            test_app,
            "skills_validate_zip",
            {"zip_data": zip_data},
        )
        assert isinstance(result, dict)
        assert result.get("valid") is True

    def test_validate_invalid_zip(self, test_app):
        """Validate an invalid ZIP returns valid=False."""
        bad_data = base64.b64encode(b"not-a-zip").decode()
        result = rpc_result(
            test_app,
            "skills_validate_zip",
            {"zip_data": bad_data},
        )
        assert isinstance(result, dict)
        assert result.get("valid") is False


class TestSkillsImportE2E:
    """skills_import through FastAPI async dispatch."""

    def test_import_skill(self, test_app):
        """Import a .skill ZIP package."""
        zip_data = _make_skill_zip("ImportedSkill", "Imported via e2e")
        result = rpc_result(
            test_app,
            "skills_import",
            {"zip_data": zip_data, "tier": "personal"},
        )
        assert isinstance(result, dict)
        assert "imported_skills" in result
        assert "ImportedSkill" in result["imported_skills"]


# ─── Share Links Endpoints ──────────────────────────────────────────────


class TestShareLinksE2E:
    """Share link endpoints through FastAPI async dispatch.

    Share link methods return HandlerResponse dataclass objects.
    ``ResponseType`` is an Enum whose ``__dict__`` entries all start with
    ``_``, so ``_prepare_for_orjson`` serializes it as ``{}``.  We therefore
    check the ``data`` dict directly rather than relying on ``resp_type``.
    """

    def test_create_and_list_share_link(self, test_app):
        """Create a file, create share link, list it."""
        write_file(
            test_app,
            "/zone/default/user/admin/shared-doc.txt",
            "Shared content",
        )

        result = rpc_result(
            test_app,
            "create_share_link",
            {
                "path": "/zone/default/user/admin/shared-doc.txt",
                "permission_level": "viewer",
            },
        )
        # HandlerResponse dataclass: data contains the link info
        assert "data" in result
        link_id = result["data"]["link_id"]
        assert link_id

        # List share links
        list_result = rpc_result(test_app, "list_share_links")
        assert "data" in list_result
        assert list_result["data"]["count"] >= 1

        # Get specific link
        got = rpc_result(test_app, "get_share_link", {"link_id": link_id})
        assert "data" in got
        assert got["data"]["link_id"] == link_id

    def test_revoke_share_link(self, test_app):
        """Create then revoke a share link."""
        write_file(
            test_app,
            "/zone/default/user/admin/revokable.txt",
            "Data",
        )
        result = rpc_result(
            test_app,
            "create_share_link",
            {"path": "/zone/default/user/admin/revokable.txt", "permission_level": "viewer"},
        )
        link_id = result["data"]["link_id"]

        revoked = rpc_result(test_app, "revoke_share_link", {"link_id": link_id})
        assert "data" in revoked

    def test_access_share_link(self, test_app):
        """Create a share link then access it."""
        write_file(
            test_app,
            "/zone/default/user/admin/accessible.txt",
            "Accessible content",
        )
        result = rpc_result(
            test_app,
            "create_share_link",
            {"path": "/zone/default/user/admin/accessible.txt", "permission_level": "viewer"},
        )
        link_id = result["data"]["link_id"]

        accessed = rpc_result(test_app, "access_share_link", {"link_id": link_id})
        assert "data" in accessed
        assert accessed["data"]["access_granted"] is True

    def test_get_share_link_access_logs(self, test_app):
        """Create a share link, access it, then get access logs."""
        write_file(
            test_app,
            "/zone/default/user/admin/logged.txt",
            "Logged content",
        )
        result = rpc_result(
            test_app,
            "create_share_link",
            {"path": "/zone/default/user/admin/logged.txt", "permission_level": "viewer"},
        )
        link_id = result["data"]["link_id"]

        # Access once to create a log entry
        rpc_result(test_app, "access_share_link", {"link_id": link_id})

        logs = rpc_result(test_app, "get_share_link_access_logs", {"link_id": link_id})
        assert "data" in logs
        assert logs["data"]["count"] >= 1
