# Connector Package Isolation Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add metadata-complete S3 and Slack connector manifests that prove optional connector metadata can be discovered without importing optional SDKs or implementation modules.

**Architecture:** Keep the #3830 runtime registry and mount-time dependency checker unchanged. Add metadata-only `_manifest.py` modules under `src/nexus/backends/connectors/` so the extension store and generated `extensions.json` can discover S3 and Slack metadata before importing `boto3`, `slack_sdk`, or connector implementation modules.

**Tech Stack:** Python 3.14, Pydantic v2 manifest models, pytest, `nexus.extensions.index`, existing slim-wheel smoke fixtures.

---

## File Structure

- Create `src/nexus/backends/connectors/s3/__init__.py`: metadata-only package marker for the S3 connector bundle pilot.
- Create `src/nexus/backends/connectors/s3/_manifest.py`: S3 `ConnectorManifest` pointing at existing `nexus.backends.storage.path_s3.PathS3Backend`.
- Create `src/nexus/backends/connectors/slack/_manifest.py`: Slack `ConnectorManifest` for `PathSlackBackend`.
- Modify `tests/extensions/test_store.py`: tests that S3 and Slack are metadata-complete and discovery does not import optional SDKs or implementation modules.
- Modify `tests/integration/slim/test_slim_install_smoke.py`: base slim wheel test for no-extras connector metadata discovery.
- Modify `src/nexus/extensions/_index/extensions.json`: generated index updated through `python -m nexus.extensions.index build`.

### Task 1: Metadata Discovery Tests

**Files:**
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 1: Write failing tests for S3 and Slack metadata completeness**

Append this class after `TestLegacyAdapterMetadataCompleteness` in `tests/extensions/test_store.py`:

```python
class TestConnectorPackageIsolationPilot:
    """Issue #3964 pilot: S3 and Slack expose metadata-complete manifests
    without importing optional connector SDKs or implementation modules."""

    def test_s3_and_slack_manifests_are_metadata_complete(self):
        from nexus.extensions.store import get_store, reset_store

        reset_store()
        store = get_store()

        s3 = store.get("path_s3", kind="connector")
        assert s3.metadata_complete is True
        assert s3.service_name == "s3"
        assert s3.module == "nexus.backends.storage.path_s3"
        assert s3.factory == "PathS3Backend"
        assert {d.name for d in s3.runtime_deps} == {"boto3"}
        assert s3.import_probes == ("boto3",)
        assert "bucket_name" in s3.connection_args
        assert s3.connection_args["bucket_name"].config_key == "bucket"
        assert "signed_url" in s3.capabilities
        assert "multipart_upload" in s3.capabilities

        slack = store.get("slack_connector", kind="connector")
        assert slack.metadata_complete is True
        assert slack.service_name == "slack"
        assert slack.module == "nexus.backends.connectors.slack.connector"
        assert slack.factory == "PathSlackBackend"
        assert {d.name for d in slack.runtime_deps} == {"slack-sdk", "token_manager"}
        assert slack.import_probes == ("slack_sdk",)
        assert slack.user_scoped is True
        assert "token_manager_db" in slack.connection_args
        assert slack.connection_args["provider"].default == "slack"
        assert "oauth" in slack.capabilities
        assert "readme_doc" in slack.capabilities

        reset_store()
```

- [ ] **Step 2: Write failing test for no implementation imports during discovery**

Append this method inside `TestConnectorPackageIsolationPilot`:

```python
    def test_pilot_manifest_discovery_does_not_import_optional_sdks_or_impls(self):
        import sys

        from nexus.extensions.store import get_store, reset_store

        forbidden_modules = {
            "boto3",
            "slack_sdk",
            "nexus.backends.storage.path_s3",
            "nexus.backends.transports.s3_transport",
            "nexus.backends.connectors.slack.connector",
            "nexus.backends.connectors.slack.transport",
        }
        for module_name in forbidden_modules:
            sys.modules.pop(module_name, None)

        reset_store()
        store = get_store()
        assert store.get("path_s3", kind="connector").metadata_complete is True
        assert store.get("slack_connector", kind="connector").metadata_complete is True

        imported = forbidden_modules.intersection(sys.modules)
        assert imported == set()

        reset_store()
```

- [ ] **Step 3: Run the focused tests and verify they fail for the right reason**

Run:

```bash
pytest tests/extensions/test_store.py::TestConnectorPackageIsolationPilot -q
```

Expected: the first test fails because `path_s3` and `slack_connector` still come from the legacy adapter with `metadata_complete is False`, or because `slack_connector` has no metadata-complete `_manifest.py` yet. The second test may also fail because the first assertion cannot find metadata-complete manifests.

- [ ] **Step 4: Commit the failing tests**

```bash
git add tests/extensions/test_store.py
git commit -m "test: pin connector package metadata discovery"
```

### Task 2: S3 Metadata Manifest

**Files:**
- Create: `src/nexus/backends/connectors/s3/__init__.py`
- Create: `src/nexus/backends/connectors/s3/_manifest.py`
- Test: `tests/extensions/test_store.py`

- [ ] **Step 1: Add the S3 metadata package marker**

Create `src/nexus/backends/connectors/s3/__init__.py`:

```python
"""S3 connector metadata package.

The implementation remains in nexus.backends.storage.path_s3 during the
Issue #3964 pilot. This package owns metadata-only discovery for the future
installable connector bundle.
"""

__all__: list[str] = []
```

- [ ] **Step 2: Add the S3 metadata-only manifest**

Create `src/nexus/backends/connectors/s3/_manifest.py`:

```python
"""S3 connector manifest (extension store discovery).

Imported by ``nexus.extensions.store`` for metadata-only enumeration without
loading boto3 or the S3 backend implementation. Runtime mounting continues to
use ``nexus.backends._manifest.CONNECTOR_MANIFEST`` during the #3964 pilot.
"""

from __future__ import annotations

from nexus.extensions.manifest import ConnectorManifest, RuntimeDep
from nexus.extensions.types import ArgType, ConnectionArg

MANIFEST = ConnectorManifest(
    name="path_s3",
    module="nexus.backends.storage.path_s3",
    factory="PathS3Backend",
    description="AWS S3 with direct path mapping",
    service_name="s3",
    runtime_deps=(
        RuntimeDep(
            kind="python",
            name="boto3",
            extras=("s3",),
            install_hint="pip install nexus-fs[s3]",
        ),
    ),
    import_probes=("boto3",),
    capabilities=frozenset(
        {
            "rename",
            "directory_listing",
            "path_delete",
            "streaming",
            "batch_content",
            "signed_url",
            "multipart_upload",
            "native_versioning",
            "resumable_upload",
        }
    ),
    connection_args={
        "bucket_name": ConnectionArg(
            type=ArgType.STRING,
            description="S3 bucket name",
            required=True,
            config_key="bucket",
        ),
        "region_name": ConnectionArg(
            type=ArgType.STRING,
            description="AWS region (e.g., us-east-1)",
            required=False,
            env_var="AWS_DEFAULT_REGION",
        ),
        "credentials_path": ConnectionArg(
            type=ArgType.PATH,
            description="Path to AWS credentials JSON file",
            required=False,
            secret=True,
        ),
        "prefix": ConnectionArg(
            type=ArgType.STRING,
            description="Path prefix for all files in bucket",
            required=False,
            default="",
        ),
        "access_key_id": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS access key ID",
            required=False,
            secret=True,
            env_var="AWS_ACCESS_KEY_ID",
        ),
        "secret_access_key": ConnectionArg(
            type=ArgType.PASSWORD,
            description="AWS secret access key",
            required=False,
            secret=True,
            env_var="AWS_SECRET_ACCESS_KEY",
        ),
        "session_token": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS session token (for temporary credentials)",
            required=False,
            secret=True,
            env_var="AWS_SESSION_TOKEN",
        ),
    },
    config_mapping={"bucket": "bucket_name"},
)
```

- [ ] **Step 3: Run S3-focused metadata assertions**

Run:

```bash
pytest tests/extensions/test_store.py::TestConnectorPackageIsolationPilot::test_s3_and_slack_manifests_are_metadata_complete -q
```

Expected: S3 assertions pass, Slack assertions still fail because `slack_connector` remains metadata-incomplete.

- [ ] **Step 4: Commit the S3 manifest**

```bash
git add src/nexus/backends/connectors/s3 tests/extensions/test_store.py
git commit -m "feat: add metadata manifest for s3 connector"
```

### Task 3: Slack Metadata Manifest

**Files:**
- Create: `src/nexus/backends/connectors/slack/_manifest.py`
- Test: `tests/extensions/test_store.py`

- [ ] **Step 1: Add the Slack metadata-only manifest**

Create `src/nexus/backends/connectors/slack/_manifest.py`:

```python
"""Slack connector manifest (extension store discovery).

Imported by ``nexus.extensions.store`` for metadata-only enumeration without
loading slack_sdk or the Slack connector implementation. Runtime mounting
continues to use ``nexus.backends._manifest.CONNECTOR_MANIFEST`` during the
#3964 pilot.
"""

from __future__ import annotations

from nexus.extensions.manifest import ConnectorManifest, RuntimeDep
from nexus.extensions.types import ArgType, ConnectionArg

MANIFEST = ConnectorManifest(
    name="slack_connector",
    module="nexus.backends.connectors.slack.connector",
    factory="PathSlackBackend",
    description="Slack workspace with OAuth 2.0 authentication",
    service_name="slack",
    runtime_deps=(
        RuntimeDep(
            kind="python",
            name="slack-sdk",
            extras=("slack",),
            install_hint="pip install nexus-fs[slack]",
        ),
        RuntimeDep(kind="service", name="token_manager"),
    ),
    import_probes=("slack_sdk",),
    capabilities=frozenset({"user_scoped", "token_manager", "oauth", "readme_doc"}),
    connection_args={
        "token_manager_db": ConnectionArg(
            type=ArgType.PATH,
            description="Path to TokenManager database or database URL",
            required=True,
        ),
        "user_email": ConnectionArg(
            type=ArgType.STRING,
            description="User email for OAuth lookup (None for multi-user from context)",
            required=False,
        ),
        "provider": ConnectionArg(
            type=ArgType.STRING,
            description="OAuth provider name from config",
            required=False,
            default="slack",
        ),
        "max_messages_per_channel": ConnectionArg(
            type=ArgType.INTEGER,
            description="Maximum number of messages to fetch per channel",
            required=False,
            default=100,
        ),
    },
    user_scoped=True,
)
```

- [ ] **Step 2: Run the connector package isolation store tests**

Run:

```bash
pytest tests/extensions/test_store.py::TestConnectorPackageIsolationPilot -q
```

Expected: both tests pass. `get_store()` finds metadata-complete S3 and Slack manifests, and `sys.modules` does not contain the forbidden optional SDK or implementation module names after discovery.

- [ ] **Step 3: Run the broader extension store tests**

Run:

```bash
pytest tests/extensions/test_store.py -q
```

Expected: all tests in `tests/extensions/test_store.py` pass.

- [ ] **Step 4: Commit the Slack manifest**

```bash
git add src/nexus/backends/connectors/slack/_manifest.py tests/extensions/test_store.py
git commit -m "feat: add metadata manifest for slack connector"
```

### Task 4: Generated Extension Index

**Files:**
- Modify: `src/nexus/extensions/_index/extensions.json`
- Test: `tests/extensions/test_index.py`

- [ ] **Step 1: Verify the index is stale before regenerating**

Run:

```bash
python -m nexus.extensions.index verify
```

Expected: FAIL with `DRIFT:` because S3 and Slack manifests exist but are not yet present in `src/nexus/extensions/_index/extensions.json`.

- [ ] **Step 2: Regenerate the index**

Run:

```bash
python -m nexus.extensions.index build
```

Expected: command prints a line beginning with `Wrote ` and ending with `src/nexus/extensions/_index/extensions.json`.

- [ ] **Step 3: Verify the regenerated index**

Run:

```bash
python -m nexus.extensions.index verify
```

Expected: PASS with a line beginning with `OK: ` and ending with `src/nexus/extensions/_index/extensions.json is up to date`.

- [ ] **Step 4: Run index tests**

Run:

```bash
pytest tests/extensions/test_index.py -q
```

Expected: all tests pass, including deterministic serialization and strict duplicate detection.

- [ ] **Step 5: Commit the generated index**

```bash
git add src/nexus/extensions/_index/extensions.json
git commit -m "chore: refresh extension index for connector manifests"
```

### Task 5: Mount-Time Dependency Hint Regression Tests

**Files:**
- Modify: `tests/integration/backends/test_factory_dep_check.py`

- [ ] **Step 1: Write failing real-connector dependency tests**

First replace the existing `_clean_registry` fixture in `tests/integration/backends/test_factory_dep_check.py` with a snapshot/restore fixture that also restores the optional-backend registration flag. Real connector factory calls register built-in placeholders; leaving `_optional_backends_registered=True` while clearing those placeholders would make later tests order-dependent.

```python
@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    # Snapshot + restore so real connector registration does not leak across
    # tests. BackendFactory.create("path_s3", config) triggers optional backend
    # registration, so restore both the registry contents and the one-shot flag.
    import nexus.backends as backends_mod

    items_before = dict(ConnectorRegistry._base._items)
    registered_before = backends_mod._optional_backends_registered
    yield
    ConnectorRegistry._base._items.clear()
    ConnectorRegistry._base._items.update(items_before)
    backends_mod._optional_backends_registered = registered_before
```

Then append these methods to `TestFactoryDepCheck` in the same file:

```python
    def test_path_s3_missing_boto3_uses_s3_extra_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "boto3":
                return None
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("path_s3", {"bucket": "example"})

        msg = str(exc_info.value)
        assert "path_s3" in msg
        assert "boto3" in msg
        assert "pip install nexus-fs[s3]" in msg

    def test_slack_missing_sdk_and_token_manager_are_enumerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "slack_sdk":
                return None
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._service_available",
            lambda name: False if name == "token_manager" else True,
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("slack_connector", {"token_manager_db": "tokens.db"})

        msg = str(exc_info.value)
        assert "slack_connector" in msg
        assert "slack_sdk" in msg
        assert "pip install nexus-fs[slack]" in msg
        assert "service 'token_manager'" in msg
```

- [ ] **Step 2: Run the real-connector dependency tests**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py::TestFactoryDepCheck::test_path_s3_missing_boto3_uses_s3_extra_hint tests/integration/backends/test_factory_dep_check.py::TestFactoryDepCheck::test_slack_missing_sdk_and_token_manager_are_enumerated -q
```

Expected: tests pass because the existing #3830 mount-time dependency checker already gates both connectors through `CONNECTOR_MANIFEST`.

- [ ] **Step 3: Run the full factory dependency check file**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit the dependency hint tests**

```bash
git add tests/integration/backends/test_factory_dep_check.py
git commit -m "test: pin connector dependency hints for s3 and slack"
```

### Task 6: Slim No-Extras Discovery Coverage

**Files:**
- Modify: `tests/integration/slim/test_slim_install_smoke.py`

- [ ] **Step 1: Write failing slim-base discovery test**

Append this test after `test_slim_base_module_imports` in `tests/integration/slim/test_slim_install_smoke.py`:

```python
def test_slim_base_connector_metadata_discovery_without_optional_deps(
    slim_base_venv: Path,
) -> None:
    """Base slim install lists S3 and Slack metadata without connector extras."""
    script = """
import sys

for name in (
    "boto3",
    "slack_sdk",
    "nexus.backends.storage.path_s3",
    "nexus.backends.transports.s3_transport",
    "nexus.backends.connectors.slack.connector",
    "nexus.backends.connectors.slack.transport",
):
    sys.modules.pop(name, None)

import nexus
import nexus.backends
from nexus.extensions.store import get_store, reset_store

reset_store()
store = get_store()
s3 = store.get("path_s3", kind="connector")
slack = store.get("slack_connector", kind="connector")

assert s3.metadata_complete is True
assert s3.service_name == "s3"
assert "boto3" in {d.name for d in s3.runtime_deps}
assert slack.metadata_complete is True
assert slack.service_name == "slack"
assert "slack-sdk" in {d.name for d in slack.runtime_deps}

for name in (
    "boto3",
    "slack_sdk",
    "nexus.backends.storage.path_s3",
    "nexus.backends.transports.s3_transport",
    "nexus.backends.connectors.slack.connector",
    "nexus.backends.connectors.slack.transport",
):
    assert name not in sys.modules, name

print("DISCOVERY OK")
"""
    result = run_in_slim_venv(slim_base_venv, script)
    assert result.returncode == 0, (
        "slim base connector metadata discovery failed:\\n"
        f"STDOUT:\\n{result.stdout}\\nSTDERR:\\n{result.stderr}"
    )
    assert "DISCOVERY OK" in result.stdout
```

- [ ] **Step 2: Run the slim-base discovery test**

Run:

```bash
pytest tests/integration/slim/test_slim_install_smoke.py::test_slim_base_connector_metadata_discovery_without_optional_deps -v --override-ini="addopts="
```

Expected: test passes after the manifests and generated index are in place. It may take several minutes because the fixture builds and installs the slim wheel.

- [ ] **Step 3: Commit slim discovery coverage**

```bash
git add tests/integration/slim/test_slim_install_smoke.py
git commit -m "test: cover slim connector metadata without extras"
```

### Task 7: Final Verification

**Files:**
- Verify: `src/nexus/backends/connectors/s3/__init__.py`
- Verify: `src/nexus/backends/connectors/s3/_manifest.py`
- Verify: `src/nexus/backends/connectors/slack/_manifest.py`
- Verify: `tests/extensions/test_store.py`
- Verify: `tests/integration/backends/test_factory_dep_check.py`
- Verify: `tests/integration/slim/test_slim_install_smoke.py`
- Verify: `src/nexus/extensions/_index/extensions.json`

- [ ] **Step 1: Run focused extension tests**

Run:

```bash
pytest tests/extensions/test_store.py::TestConnectorPackageIsolationPilot tests/extensions/test_index.py -q
```

Expected: all selected extension tests pass.

- [ ] **Step 2: Run mount-time dependency tests**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py -q
```

Expected: all factory dependency tests pass.

- [ ] **Step 3: Verify generated extension index**

Run:

```bash
python -m nexus.extensions.index verify
```

Expected: a line beginning with `OK: ` and ending with `src/nexus/extensions/_index/extensions.json is up to date`.

- [ ] **Step 4: Run slim no-extras discovery test**

Run:

```bash
pytest tests/integration/slim/test_slim_install_smoke.py::test_slim_base_connector_metadata_discovery_without_optional_deps -v --override-ini="addopts="
```

Expected: the test passes with `DISCOVERY OK` in subprocess stdout.

- [ ] **Step 5: Check formatting and whitespace**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 6: Inspect final diff**

Run:

```bash
git diff --stat HEAD
```

Expected: diff includes only the S3/Slack manifest files, connector metadata tests, slim discovery test, and generated extension index.
