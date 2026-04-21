# Universal OAuth + Lift-to-lib Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `src/nexus/lib/oauth/` the canonical OAuth code location shared by both `nexus-fs` slim and `nexus-ai-fs` full packages. Add RFC 8414 discovery + a `UniversalOAuthProvider` base + PKCE in base class + a working Slack provider. Fix the three broken `provider_class` paths in `configs/oauth.yaml`.

**Architecture:**
- Pure universal code (base class, types, protocol, crypto, PKCE, discovery, universal provider, provider subclasses) lives in `src/nexus/lib/oauth/`. Ships in both slim and full wheels (`nexus/lib/**` is already in slim include list).
- Storage/orchestration/registration (`token_manager.py`, `credential_service.py`, `factory.py`, `pending.py`, `user_auth.py`, `token_resolver.py`) stays in `src/nexus/bricks/auth/oauth/` — full-only.
- Existing provider classes keep their vendor quirks as thin subclasses of `UniversalOAuthProvider`. New RFC-compliant providers can be added as pure YAML entries pointing at a discovery URL.
- Brick-side `base_provider.py` / `types.py` / `protocol.py` / `crypto.py` / `providers/*` become compatibility re-exports so every existing importer keeps working.

**Tech Stack:** Python 3.14, httpx (added to nexus-fs slim base deps), cryptography (already slim), pydantic (already slim), pytest + pytest-asyncio, pytest-httpx for mocking.

**Scope:** Stacked onto PR #3815 (current branch: `worktree-atomic-singing-cook`). Same branch; no new PR.

---

## File Structure (end state)

```
src/nexus/lib/oauth/                 # ← NEW (slim+full)
├── __init__.py                      # re-exports public API
├── types.py                         # OAuthCredential, OAuthError, PendingOAuthRegistration
├── protocol.py                      # OAuthProviderProtocol, OAuthTokenManagerProtocol
├── crypto.py                        # OAuthCrypto (Fernet)
├── pkce.py                          # generate_pkce_pair, make_code_challenge
├── base.py                          # BaseOAuthProvider (w/ optional PKCE)
├── discovery.py                     # DiscoveryClient (RFC 8414)
├── universal.py                     # UniversalOAuthProvider
├── providers/
│   ├── __init__.py
│   ├── google.py                    # GoogleOAuthProvider (extends Universal)
│   ├── microsoft.py                 # MicrosoftOAuthProvider
│   ├── x.py                         # XOAuthProvider
│   └── slack.py                     # SlackOAuthProvider (new)
└── tests/
    ├── __init__.py
    ├── test_pkce.py
    ├── test_discovery.py
    ├── test_universal.py
    ├── test_base_pkce.py
    └── test_slack_provider.py

src/nexus/bricks/auth/oauth/         # still exists, now thin
├── __init__.py                      # unchanged
├── types.py                         # re-export from lib
├── protocol.py                      # re-export from lib
├── crypto.py                        # re-export from lib
├── base_provider.py                 # re-export BaseOAuthProvider from lib
├── config.py                        # unchanged (compat shim)
├── pending.py                       # stays here (needs cachetools; server-only)
├── token_manager.py                 # stays (imports updated to lib types)
├── credential_service.py            # stays (imports updated)
├── factory.py                       # stays (imports updated)
├── user_auth.py                     # stays (imports updated)
├── token_resolver.py                # stays (imports updated)
└── providers/
    ├── google.py                    # re-export from lib.oauth.providers.google
    ├── microsoft.py                 # re-export
    ├── x.py                         # re-export
    └── slack.py                     # re-export

configs/oauth.yaml                   # provider_class paths updated to lib paths
packages/nexus-fs/pyproject.toml     # httpx>=0.28 added to slim base deps
```

**Do not move:** `pending.py` (depends on `cachetools`, not in slim base deps, and only used by server-side registration flow — no slim consumer).

---

## Task 1: Preflight — snapshot current state

**Files:**
- Read-only: `configs/oauth.yaml`, `packages/nexus-fs/pyproject.toml`, `pyproject.toml`

- [ ] **Step 1: Record baseline test counts**

Run both suites and record PASS counts so the refactor can be validated as non-regressing.

```bash
pytest src/nexus/bricks/auth/oauth/tests -q 2>&1 | tail -5
pytest tests/unit/fs/test_oauth_support.py -q 2>&1 | tail -5 || true
```

Expected: Record number of passed tests; there should be zero failures before starting.

- [ ] **Step 2: Confirm current branch + worktree**

```bash
git rev-parse --abbrev-ref HEAD
git status --short
```

Expected: branch `worktree-atomic-singing-cook`, clean tree.

---

## Task 2: Add `httpx` to slim base deps + create `src/nexus/lib/oauth/` package skeleton

**Files:**
- Modify: `packages/nexus-fs/pyproject.toml:32-43` (add httpx)
- Create: `src/nexus/lib/oauth/__init__.py`
- Create: `src/nexus/lib/oauth/tests/__init__.py`

- [ ] **Step 1: Add httpx to slim base deps**

Edit `packages/nexus-fs/pyproject.toml`, find the base `dependencies` list:

```toml
dependencies = [
    "pydantic[email]>=2.0",
    "click>=8.0",
    "rich>=13.0",
    "orjson>=3.9",
    "blake3>=0.4",
    "anyio>=4.0",
    "aiofiles>=23.0",
    "pyyaml>=6.0",
    "platformdirs>=4.0",
    "cryptography>=41.0",
    "httpx>=0.28",
]
```

Update the `# ~16 base dependencies` comment to `# ~17 base dependencies`.

- [ ] **Step 2: Create lib/oauth package init**

Write `src/nexus/lib/oauth/__init__.py`:

```python
"""Universal OAuth primitives shared by nexus-fs slim and nexus-ai-fs full.

This package ships in both wheels. Storage-backed orchestration (token manager,
credential service, factory) stays in ``nexus.bricks.auth.oauth``.
"""

from nexus.lib.oauth.base import BaseOAuthProvider
from nexus.lib.oauth.crypto import OAuthCrypto
from nexus.lib.oauth.discovery import DiscoveryClient, DiscoveryMetadata
from nexus.lib.oauth.pkce import generate_pkce_pair, make_code_challenge
from nexus.lib.oauth.protocol import OAuthProviderProtocol, OAuthTokenManagerProtocol
from nexus.lib.oauth.types import OAuthCredential, OAuthError, PendingOAuthRegistration
from nexus.lib.oauth.universal import UniversalOAuthProvider

__all__ = [
    "BaseOAuthProvider",
    "DiscoveryClient",
    "DiscoveryMetadata",
    "OAuthCredential",
    "OAuthCrypto",
    "OAuthError",
    "OAuthProviderProtocol",
    "OAuthTokenManagerProtocol",
    "PendingOAuthRegistration",
    "UniversalOAuthProvider",
    "generate_pkce_pair",
    "make_code_challenge",
]
```

- [ ] **Step 3: Create test package init**

Write `src/nexus/lib/oauth/tests/__init__.py`:

```python
```

(Empty file, just makes it a package.)

- [ ] **Step 4: Verify slim wheel includes lib/**

Open `packages/nexus-fs/pyproject.toml` and confirm the include list contains `"nexus/lib/**"` (line ~92). If missing, add it. (As of 2026-04-19 it is already present.)

- [ ] **Step 5: Sanity check — package is importable (will fail until later tasks)**

```bash
python3 -c "import sys; sys.path.insert(0,'src'); import nexus.lib.oauth" 2>&1 | head -5
```

Expected: ImportError for one of the submodules we haven't created yet. That's fine.

- [ ] **Step 6: Commit**

```bash
git add packages/nexus-fs/pyproject.toml src/nexus/lib/oauth/__init__.py src/nexus/lib/oauth/tests/__init__.py
git commit -m "feat(lib/oauth): scaffold lib.oauth package + add httpx to slim base"
```

---

## Task 3: Move `types.py` to `lib/oauth/`

**Files:**
- Create: `src/nexus/lib/oauth/types.py` (copy of current brick types)
- Modify: `src/nexus/bricks/auth/oauth/types.py` → re-export

- [ ] **Step 1: Copy types.py to lib**

Write `src/nexus/lib/oauth/types.py` — this is byte-for-byte the current `src/nexus/bricks/auth/oauth/types.py` (all 128 lines, unchanged). The file already has no brick-path imports, so no editing is required.

- [ ] **Step 2: Replace brick types.py with re-export shim**

Overwrite `src/nexus/bricks/auth/oauth/types.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.types``."""

from nexus.lib.oauth.types import (
    OAuthCredential,
    OAuthError,
    PendingOAuthRegistration,
    _mask_token,
)

__all__ = [
    "OAuthCredential",
    "OAuthError",
    "PendingOAuthRegistration",
    "_mask_token",
]
```

- [ ] **Step 3: Run brick unit tests to verify no import breakage**

```bash
pytest src/nexus/bricks/auth/oauth/tests/test_types.py -q 2>&1 | tail -5
```

Expected: all existing tests pass (same count as baseline from Task 1).

- [ ] **Step 4: Commit**

```bash
git add src/nexus/lib/oauth/types.py src/nexus/bricks/auth/oauth/types.py
git commit -m "refactor(lib/oauth): move types to lib; brick now re-exports"
```

---

## Task 4: Move `protocol.py` to `lib/oauth/`

**Files:**
- Create: `src/nexus/lib/oauth/protocol.py`
- Modify: `src/nexus/bricks/auth/oauth/protocol.py` → re-export

- [ ] **Step 1: Copy protocol.py to lib with lib-side import**

Write `src/nexus/lib/oauth/protocol.py` — copy current brick `protocol.py`, change the single import on line 9 from `nexus.bricks.auth.oauth.types` to `nexus.lib.oauth.types`. Everything else unchanged.

- [ ] **Step 2: Replace brick protocol.py with re-export**

Overwrite `src/nexus/bricks/auth/oauth/protocol.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.protocol``."""

from nexus.lib.oauth.protocol import OAuthProviderProtocol, OAuthTokenManagerProtocol

__all__ = ["OAuthProviderProtocol", "OAuthTokenManagerProtocol"]
```

- [ ] **Step 3: Commit**

```bash
git add src/nexus/lib/oauth/protocol.py src/nexus/bricks/auth/oauth/protocol.py
git commit -m "refactor(lib/oauth): move protocol to lib; brick now re-exports"
```

---

## Task 5: Move `crypto.py` to `lib/oauth/`

**Files:**
- Create: `src/nexus/lib/oauth/crypto.py`
- Modify: `src/nexus/bricks/auth/oauth/crypto.py` → re-export

The brick `crypto.py` imports `nexus.contracts.auth_store_protocols.SystemSettingsStoreProtocol` only inside `TYPE_CHECKING`. `nexus.contracts` ships in both slim and full, so the move is safe.

- [ ] **Step 1: Copy crypto.py to lib**

Write `src/nexus/lib/oauth/crypto.py` — copy current brick `crypto.py` byte-for-byte (no import changes needed).

- [ ] **Step 2: Replace brick crypto.py with re-export**

Overwrite `src/nexus/bricks/auth/oauth/crypto.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.crypto``."""

from nexus.lib.oauth.crypto import (
    OAUTH_ENCRYPTION_KEY_ENV,
    OAUTH_ENCRYPTION_KEY_NAME,
    OAuthCrypto,
)

__all__ = [
    "OAUTH_ENCRYPTION_KEY_ENV",
    "OAUTH_ENCRYPTION_KEY_NAME",
    "OAuthCrypto",
]
```

- [ ] **Step 3: Run crypto tests**

```bash
pytest src/nexus/bricks/auth/oauth/tests/test_crypto.py -q 2>&1 | tail -5
```

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/lib/oauth/crypto.py src/nexus/bricks/auth/oauth/crypto.py
git commit -m "refactor(lib/oauth): move crypto to lib; brick now re-exports"
```

---

## Task 6: Add PKCE helpers in `lib/oauth/pkce.py` (TDD)

**Files:**
- Create: `src/nexus/lib/oauth/pkce.py`
- Test: `src/nexus/lib/oauth/tests/test_pkce.py`

- [ ] **Step 1: Write failing test**

Write `src/nexus/lib/oauth/tests/test_pkce.py`:

```python
"""Tests for RFC 7636 PKCE helpers."""

import base64
import hashlib

from nexus.lib.oauth.pkce import generate_pkce_pair, make_code_challenge


def test_generate_pkce_pair_returns_verifier_and_challenge() -> None:
    verifier, challenge = generate_pkce_pair()
    assert isinstance(verifier, str)
    assert isinstance(challenge, str)
    assert len(verifier) >= 43  # RFC 7636 §4.1 minimum
    assert len(verifier) <= 128  # RFC 7636 §4.1 maximum


def test_code_challenge_matches_spec() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    # From RFC 7636 appendix B
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert make_code_challenge(verifier) == expected


def test_challenge_is_sha256_urlsafe_b64_no_padding() -> None:
    verifier, challenge = generate_pkce_pair()
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pairs_are_unique() -> None:
    pairs = {generate_pkce_pair() for _ in range(20)}
    assert len(pairs) == 20
```

- [ ] **Step 2: Run the test (expect failure)**

```bash
pytest src/nexus/lib/oauth/tests/test_pkce.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'nexus.lib.oauth.pkce'`.

- [ ] **Step 3: Implement pkce.py**

Write `src/nexus/lib/oauth/pkce.py`:

```python
"""RFC 7636 PKCE helpers.

S256-only. The plain ``code_challenge_method`` is not supported because every
OAuth 2.1 server allows S256 and some reject plain.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

__all__ = ["generate_pkce_pair", "make_code_challenge"]


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)``.

    ``code_verifier`` is a 32-byte urlsafe-base64 string without padding
    (43 chars), satisfying RFC 7636 §4.1. ``code_challenge`` is
    ``SHA256(verifier)`` urlsafe-base64 without padding.
    """
    verifier = secrets.token_urlsafe(32)
    return verifier, make_code_challenge(verifier)


def make_code_challenge(verifier: str) -> str:
    """Compute the S256 code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
```

- [ ] **Step 4: Run the test (expect pass)**

```bash
pytest src/nexus/lib/oauth/tests/test_pkce.py -v 2>&1 | tail -10
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/oauth/pkce.py src/nexus/lib/oauth/tests/test_pkce.py
git commit -m "feat(lib/oauth): add RFC 7636 PKCE helpers (S256)"
```

---

## Task 7: Move `base_provider.py` to `lib/oauth/base.py` + add optional PKCE in base

**Files:**
- Create: `src/nexus/lib/oauth/base.py`
- Modify: `src/nexus/bricks/auth/oauth/base_provider.py` → re-export
- Test: `src/nexus/lib/oauth/tests/test_base_pkce.py`

This is a move + a behavior addition: `BaseOAuthProvider` gains a `requires_pkce: bool = False` class attribute and two new methods (`get_authorization_url_with_pkce` / `exchange_code_pkce`). Non-PKCE providers behave identically to today.

- [ ] **Step 1: Write failing test for PKCE in base**

Write `src/nexus/lib/oauth/tests/test_base_pkce.py`:

```python
"""Tests for optional PKCE support in BaseOAuthProvider."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.lib.oauth.base import BaseOAuthProvider
from nexus.lib.oauth.types import OAuthCredential


class _DummyProvider(BaseOAuthProvider):
    TOKEN_ENDPOINT = "https://example.com/token"
    AUTHORIZATION_ENDPOINT = "https://example.com/auth"
    requires_pkce = True

    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str:
        url, _ = self.get_authorization_url_with_pkce(state=state)
        return url

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        code_verifier = kwargs.get("code_verifier")
        if code_verifier:
            params["code_verifier"] = code_verifier
        return params

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token or "",
            "client_id": self.client_id,
        }

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        return True

    async def validate_token(self, access_token: str) -> bool:
        return True


def test_pkce_url_contains_code_challenge_s256() -> None:
    provider = _DummyProvider(
        client_id="cid",
        client_secret="",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="dummy",
    )
    url, pkce = provider.get_authorization_url_with_pkce()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"][0] == pkce["code_challenge"]
    assert len(pkce["code_verifier"]) >= 43


def test_pkce_requires_verifier_on_exchange() -> None:
    provider = _DummyProvider(
        client_id="cid",
        client_secret="",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="dummy",
    )
    with pytest.raises(Exception) as exc_info:
        # exchange_code without code_verifier must raise when requires_pkce=True
        import asyncio

        asyncio.run(provider.exchange_code("abc"))
    assert "code_verifier" in str(exc_info.value).lower() or "pkce" in str(exc_info.value).lower()
```

- [ ] **Step 2: Run test (expect failure)**

```bash
pytest src/nexus/lib/oauth/tests/test_base_pkce.py -v 2>&1 | tail -15
```

Expected: ImportError for `nexus.lib.oauth.base`.

- [ ] **Step 3: Create `lib/oauth/base.py`**

Write `src/nexus/lib/oauth/base.py` — based on the current `src/nexus/bricks/auth/oauth/base_provider.py` with these changes:

1. Change import `from nexus.bricks.auth.oauth.types` → `from nexus.lib.oauth.types`.
2. Add `requires_pkce: bool = False` class attribute.
3. Add `AUTHORIZATION_ENDPOINT` to the class (subclasses may override; default empty string to keep abstract-but-settable).
4. Add `get_authorization_url_with_pkce()` method.
5. Change `exchange_code()` to pass `**kwargs` including optional `code_verifier` through to `_build_exchange_params`, and to raise `OAuthError` if `requires_pkce` is True and `code_verifier` is not provided.
6. Add `exchange_code_pkce()` wrapper that calls `exchange_code(code, code_verifier=...)`.

Full new file:

```python
"""Template Method base for OAuth providers (RFC 6749 + optional RFC 7636 PKCE).

Shared behavior:
- Token exchange and refresh HTTP POSTs with unified error handling
- Standard ``access_token`` / ``refresh_token`` / ``expires_in`` response parsing
- Optional PKCE: set ``requires_pkce = True`` on the subclass to require a
  ``code_verifier`` in :meth:`exchange_code`.

Subclasses must define:
- ``TOKEN_ENDPOINT`` — token exchange/refresh URL
- ``AUTHORIZATION_ENDPOINT`` — user-consent URL (may be empty for client-credentials)
- ``get_authorization_url()`` — builds the authorize-redirect URL
- ``_build_exchange_params()`` — POST body for ``exchange_code``
- ``_build_refresh_params()`` — POST body for ``refresh_token``
- ``revoke_token()`` / ``validate_token()`` — vendor endpoints
"""

from __future__ import annotations

import dataclasses
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.lib.oauth.pkce import generate_pkce_pair
from nexus.lib.oauth.types import OAuthCredential, OAuthError

logger = logging.getLogger(__name__)


class BaseOAuthProvider(ABC):
    """Template Method base for all OAuth providers."""

    TOKEN_ENDPOINT: str = ""
    AUTHORIZATION_ENDPOINT: str = ""

    # RFC 7636: subclasses that MUST use PKCE flip this to True.
    # Providers that CAN use PKCE but don't require it can still call
    # ``get_authorization_url_with_pkce`` / ``exchange_code_pkce`` directly.
    requires_pkce: bool = False

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not redirect_uri:
            raise OAuthError("redirect_uri is required for OAuth provider")
        if not scopes:
            raise OAuthError("At least one scope is required for OAuth provider")
        if not provider_name:
            raise OAuthError("provider_name is required for OAuth provider")

        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.provider_name = provider_name
        self._http_client = http_client

    @asynccontextmanager
    async def _get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient() as client:
                yield client

    # ── PKCE helpers (optional) ─────────────────────────────────

    def get_authorization_url_with_pkce(
        self,
        state: str | None = None,
        *,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Build an authorization URL with PKCE ``code_challenge``.

        Returns ``(url, pkce_data)`` where ``pkce_data`` has ``code_verifier``,
        ``code_challenge``, and ``state``. The caller must persist
        ``code_verifier`` and pass it back to :meth:`exchange_code_pkce`.
        """
        if not self.AUTHORIZATION_ENDPOINT:
            raise OAuthError(
                f"{type(self).__name__} has no AUTHORIZATION_ENDPOINT set"
            )
        import secrets as _secrets

        verifier, challenge = generate_pkce_pair()
        state = state or _secrets.token_urlsafe(32)
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self._scope_string(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if extra_params:
            params.update(extra_params)
        url = f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"
        return url, {
            "code_verifier": verifier,
            "code_challenge": challenge,
            "state": state,
        }

    async def exchange_code_pkce(
        self, code: str, code_verifier: str, **kwargs: Any
    ) -> OAuthCredential:
        """PKCE-aware wrapper around :meth:`exchange_code`."""
        return await self.exchange_code(code, code_verifier=code_verifier, **kwargs)

    # ── Template Method: exchange_code ──────────────────────────

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthCredential:
        if self.requires_pkce and "code_verifier" not in kwargs:
            raise OAuthError(
                f"{self.provider_name} requires PKCE; call exchange_code_pkce or "
                f"pass code_verifier kwarg."
            )
        params = self._build_exchange_params(code, **kwargs)
        headers = self._build_exchange_headers()
        token_data = await self._post_token_request(
            params, headers=headers, action="exchange code"
        )
        return self._parse_token_response(token_data)

    @abstractmethod
    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]: ...

    def _build_exchange_headers(self) -> dict[str, str] | None:
        return None

    # ── Template Method: refresh_token ──────────────────────────

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        if not credential.refresh_token:
            raise OAuthError("No refresh_token available")

        params = self._build_refresh_params(credential)
        headers = self._build_refresh_headers()
        token_data = await self._post_token_request(
            params, headers=headers, action="refresh token"
        )
        new_cred = self._parse_token_response(token_data)
        refresh = new_cred.refresh_token or credential.refresh_token
        return dataclasses.replace(
            new_cred,
            refresh_token=refresh,
            provider=self.provider_name,
            user_email=credential.user_email,
            scopes=credential.scopes or new_cred.scopes,
        )

    @abstractmethod
    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]: ...

    def _build_refresh_headers(self) -> dict[str, str] | None:
        return None

    # ── Abstract methods: provider-specific ─────────────────────

    @abstractmethod
    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str: ...

    @abstractmethod
    async def revoke_token(self, credential: OAuthCredential) -> bool: ...

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool: ...

    # ── Shared infrastructure ──────────────────────────────────

    def _scope_string(self) -> str:
        """Serialize ``self.scopes`` for authorization/token URLs.

        Default is space-separated (RFC 6749). Subclasses that need a
        different separator (Slack uses comma, see
        https://api.slack.com/authentication/oauth-v2) override this.
        """
        return " ".join(self.scopes)

    async def _post_token_request(
        self,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        action: str = "token request",
    ) -> dict[str, Any]:
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.TOKEN_ENDPOINT, data=data, headers=headers
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as e:
                raise OAuthError(f"Failed to {action}: {e.response.text}") from e
            except Exception as e:
                raise OAuthError(f"Failed to {action}: {e}") from e

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        expires_at = None
        if "expires_in" in token_data:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=int(token_data["expires_in"])
            )

        scopes = None
        if "scope" in token_data:
            scopes = tuple(token_data["scope"].split(" "))

        return OAuthCredential(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
            provider=self.provider_name,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
        )
```

- [ ] **Step 4: Run PKCE base test (expect pass)**

```bash
pytest src/nexus/lib/oauth/tests/test_base_pkce.py -v 2>&1 | tail -10
```

Expected: 2 tests pass.

- [ ] **Step 5: Replace brick base_provider.py with re-export**

Overwrite `src/nexus/bricks/auth/oauth/base_provider.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.base``."""

from nexus.lib.oauth.base import BaseOAuthProvider

__all__ = ["BaseOAuthProvider"]
```

- [ ] **Step 6: Run full brick OAuth test suite**

```bash
pytest src/nexus/bricks/auth/oauth/tests -q 2>&1 | tail -5
```

Expected: same pass count as baseline from Task 1. No regressions.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/lib/oauth/base.py \
        src/nexus/lib/oauth/tests/test_base_pkce.py \
        src/nexus/bricks/auth/oauth/base_provider.py
git commit -m "feat(lib/oauth): base provider with optional PKCE in lib; brick re-exports"
```

---

## Task 8: RFC 8414 discovery client (TDD)

**Files:**
- Create: `src/nexus/lib/oauth/discovery.py`
- Test: `src/nexus/lib/oauth/tests/test_discovery.py`

Uses httpx (already a slim dep as of Task 2). Returns a frozen `DiscoveryMetadata` dataclass.

- [ ] **Step 1: Write failing test**

Write `src/nexus/lib/oauth/tests/test_discovery.py`:

```python
"""Tests for RFC 8414 OAuth 2.0 Authorization Server Metadata discovery."""

from __future__ import annotations

import json

import httpx
import pytest

from nexus.lib.oauth.discovery import DiscoveryClient, DiscoveryError, DiscoveryMetadata


_SAMPLE_METADATA = {
    "issuer": "https://issuer.example",
    "authorization_endpoint": "https://issuer.example/oauth2/authorize",
    "token_endpoint": "https://issuer.example/oauth2/token",
    "revocation_endpoint": "https://issuer.example/oauth2/revoke",
    "registration_endpoint": "https://issuer.example/oauth2/register",
    "scopes_supported": ["read", "write", "openid"],
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256", "plain"],
}


@pytest.mark.asyncio
async def test_fetch_parses_well_known_oauth_authorization_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=_SAMPLE_METADATA)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await DiscoveryClient(client=client).fetch("https://issuer.example")

    assert isinstance(meta, DiscoveryMetadata)
    assert meta.authorization_endpoint == _SAMPLE_METADATA["authorization_endpoint"]
    assert meta.token_endpoint == _SAMPLE_METADATA["token_endpoint"]
    assert meta.revocation_endpoint == _SAMPLE_METADATA["revocation_endpoint"]
    assert meta.scopes_supported == tuple(_SAMPLE_METADATA["scopes_supported"])
    assert "S256" in meta.code_challenge_methods_supported


@pytest.mark.asyncio
async def test_fetch_falls_back_to_openid_configuration() -> None:
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.path)
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(404)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=_SAMPLE_METADATA)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await DiscoveryClient(client=client).fetch("https://issuer.example")

    assert call_log == [
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ]
    assert meta.token_endpoint == _SAMPLE_METADATA["token_endpoint"]


@pytest.mark.asyncio
async def test_fetch_rejects_issuer_mismatch() -> None:
    payload = dict(_SAMPLE_METADATA, issuer="https://other.example")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DiscoveryError) as exc_info:
            await DiscoveryClient(client=client).fetch("https://issuer.example")
    assert "issuer" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_fetch_times_out_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DiscoveryError):
            await DiscoveryClient(client=client, timeout=0.1).fetch(
                "https://issuer.example"
            )


def test_metadata_rejects_missing_required_fields() -> None:
    with pytest.raises(DiscoveryError):
        DiscoveryMetadata.from_dict({"issuer": "x"})  # missing endpoints
```

- [ ] **Step 2: Run test (expect failure)**

```bash
pytest src/nexus/lib/oauth/tests/test_discovery.py -v 2>&1 | tail -15
```

Expected: ImportError or collection errors for missing module.

- [ ] **Step 3: Implement discovery.py**

Write `src/nexus/lib/oauth/discovery.py`:

```python
"""RFC 8414 OAuth 2.0 Authorization Server Metadata + OIDC Discovery.

Fetches ``/.well-known/oauth-authorization-server``. If that returns 404 or
non-JSON, falls back to ``/.well-known/openid-configuration`` (OIDC Discovery),
which carries the same endpoint fields for most real-world providers
(Auth0, Okta, Keycloak, Google).

Usage:

    client = DiscoveryClient()
    meta = await client.fetch("https://accounts.google.com")
    provider = UniversalOAuthProvider(
        client_id=..., client_secret=..., scopes=["openid"],
        provider_name="google-oidc",
        discovery_metadata=meta,
    )
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """Discovery fetch or parse failed."""


@dataclass(frozen=True, slots=True)
class DiscoveryMetadata:
    """Parsed RFC 8414 / OIDC Discovery metadata."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None
    introspection_endpoint: str | None = None
    registration_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    jwks_uri: str | None = None
    scopes_supported: tuple[str, ...] = ()
    response_types_supported: tuple[str, ...] = ()
    grant_types_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryMetadata":
        required = ("issuer", "authorization_endpoint", "token_endpoint")
        missing = [k for k in required if not data.get(k)]
        if missing:
            raise DiscoveryError(
                f"Discovery document missing required field(s): {', '.join(missing)}"
            )
        return cls(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            revocation_endpoint=data.get("revocation_endpoint"),
            introspection_endpoint=data.get("introspection_endpoint"),
            registration_endpoint=data.get("registration_endpoint"),
            userinfo_endpoint=data.get("userinfo_endpoint"),
            jwks_uri=data.get("jwks_uri"),
            scopes_supported=tuple(data.get("scopes_supported") or ()),
            response_types_supported=tuple(data.get("response_types_supported") or ()),
            grant_types_supported=tuple(data.get("grant_types_supported") or ()),
            code_challenge_methods_supported=tuple(
                data.get("code_challenge_methods_supported") or ()
            ),
            token_endpoint_auth_methods_supported=tuple(
                data.get("token_endpoint_auth_methods_supported") or ()
            ),
            raw=data,
        )


_WELL_KNOWN_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


class DiscoveryClient:
    """Fetch + parse authorization server metadata."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, issuer_url: str) -> DiscoveryMetadata:
        """Fetch metadata for ``issuer_url`` and validate ``issuer`` matches."""
        issuer_url = issuer_url.rstrip("/")
        last_error: Exception | None = None
        for path in _WELL_KNOWN_PATHS:
            url = f"{issuer_url}{path}"
            try:
                data = await self._fetch_one(url)
            except DiscoveryError as exc:
                last_error = exc
                continue
            meta = DiscoveryMetadata.from_dict(data)
            expected = issuer_url
            if meta.issuer.rstrip("/") != expected:
                raise DiscoveryError(
                    f"Issuer mismatch: expected {expected}, discovery returned "
                    f"{meta.issuer}"
                )
            return meta
        raise DiscoveryError(
            f"No discovery document at {issuer_url} "
            f"(tried {list(_WELL_KNOWN_PATHS)})"
        ) from last_error

    async def _fetch_one(self, url: str) -> dict[str, Any]:
        client = self._client
        opened = False
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            opened = True
        try:
            try:
                response = await client.get(url)
            except httpx.TimeoutException as exc:
                raise DiscoveryError(f"Timeout fetching {url}") from exc
            except httpx.RequestError as exc:
                raise DiscoveryError(f"Network error fetching {url}: {exc}") from exc
            if response.status_code == 404:
                raise DiscoveryError(f"{url} returned 404")
            if response.status_code >= 400:
                raise DiscoveryError(
                    f"{url} returned {response.status_code}: {response.text[:200]}"
                )
            try:
                return response.json()  # type: ignore[no-any-return]
            except _json.JSONDecodeError as exc:
                raise DiscoveryError(f"{url} returned non-JSON body") from exc
        finally:
            if opened:
                await client.aclose()
```

- [ ] **Step 4: Install `pytest-asyncio` marker shim if needed**

If the test fails with "async def functions are not natively supported", add `asyncio_mode = "auto"` to `pytest.ini` or mark tests explicitly. The repo already has `asyncio_mode = "auto"` globally, so no change should be needed.

- [ ] **Step 5: Run discovery tests**

```bash
pytest src/nexus/lib/oauth/tests/test_discovery.py -v 2>&1 | tail -15
```

Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/lib/oauth/discovery.py src/nexus/lib/oauth/tests/test_discovery.py
git commit -m "feat(lib/oauth): RFC 8414 + OIDC discovery client"
```

---

## Task 9: `UniversalOAuthProvider` (TDD)

**Files:**
- Create: `src/nexus/lib/oauth/universal.py`
- Test: `src/nexus/lib/oauth/tests/test_universal.py`

A concrete `BaseOAuthProvider` subclass that reads endpoints from either:
- A `DiscoveryMetadata` object (RFC 8414), OR
- Explicit constructor kwargs (`authorization_endpoint`, `token_endpoint`, etc.)

Plus two quirk knobs that cover 80% of real vendors:
- `scope_format: "space" | "comma" | "plus"` (default `"space"`)
- `scope_on_refresh: bool` (default `False`) — Microsoft-style scope-required-on-refresh
- Optional `requires_pkce: bool` via class attr already in base

- [ ] **Step 1: Write failing tests**

Write `src/nexus/lib/oauth/tests/test_universal.py`:

```python
"""Tests for UniversalOAuthProvider."""

from __future__ import annotations

import httpx
import pytest

from nexus.lib.oauth.discovery import DiscoveryMetadata
from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


def _meta() -> DiscoveryMetadata:
    return DiscoveryMetadata(
        issuer="https://issuer.example",
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        revocation_endpoint="https://issuer.example/revoke",
        scopes_supported=("read", "write"),
        code_challenge_methods_supported=("S256",),
    )


def test_endpoints_from_discovery_metadata() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="generic",
        discovery_metadata=_meta(),
    )
    assert provider.TOKEN_ENDPOINT == "https://issuer.example/token"
    assert provider.AUTHORIZATION_ENDPOINT == "https://issuer.example/authorize"
    assert provider.REVOKE_ENDPOINT == "https://issuer.example/revoke"


def test_endpoints_from_explicit_kwargs() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="generic",
        authorization_endpoint="https://a.example/auth",
        token_endpoint="https://a.example/token",
    )
    assert provider.TOKEN_ENDPOINT == "https://a.example/token"


def test_scope_format_space_default() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["a", "b"],
        provider_name="generic",
        discovery_metadata=_meta(),
    )
    assert provider._scope_string() == "a b"


def test_scope_format_comma() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["a", "b"],
        provider_name="generic",
        discovery_metadata=_meta(),
        scope_format="comma",
    )
    assert provider._scope_string() == "a,b"


@pytest.mark.asyncio
async def test_exchange_code_posts_standard_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = UniversalOAuthProvider(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            scopes=["read"],
            provider_name="generic",
            discovery_metadata=_meta(),
            http_client=client,
        )
        cred = await provider.exchange_code("code123")
    assert isinstance(cred, OAuthCredential)
    assert cred.access_token == "at"
    body = captured[0]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code123"
    assert body["client_id"] == "cid"
    assert body["client_secret"] == "secret"


@pytest.mark.asyncio
async def test_refresh_includes_scope_when_scope_on_refresh_true() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        captured.append(body)
        return httpx.Response(
            200,
            json={"access_token": "at2", "token_type": "Bearer", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = UniversalOAuthProvider(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            scopes=["read", "write"],
            provider_name="generic",
            discovery_metadata=_meta(),
            scope_on_refresh=True,
            http_client=client,
        )
        old = OAuthCredential(
            access_token="old",
            refresh_token="rtok",
            provider="generic",
            scopes=("read", "write"),
        )
        await provider.refresh_token(old)

    assert "scope" in captured[0]
    assert captured[0]["scope"] in ("read+write", "read%20write", "read write")


def test_requires_pkce_can_be_set_via_ctor() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="",
        redirect_uri="http://localhost/cb",
        scopes=["read"],
        provider_name="generic",
        discovery_metadata=_meta(),
        requires_pkce=True,
    )
    assert provider.requires_pkce is True
```

- [ ] **Step 2: Run test (expect failure)**

```bash
pytest src/nexus/lib/oauth/tests/test_universal.py -v 2>&1 | tail -15
```

Expected: ImportError for `nexus.lib.oauth.universal`.

- [ ] **Step 3: Implement `universal.py`**

Write `src/nexus/lib/oauth/universal.py`:

```python
"""Universal RFC 6749 OAuth provider.

Use when the target provider is RFC 6749 compliant and endpoints are either
published via RFC 8414 discovery or can be supplied explicitly. Vendor quirks
that cannot be captured by the two knobs here (``scope_format``,
``scope_on_refresh``) should live in a dedicated subclass — see
``nexus.lib.oauth.providers`` for examples.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.lib.oauth.base import BaseOAuthProvider
from nexus.lib.oauth.discovery import DiscoveryMetadata
from nexus.lib.oauth.types import OAuthCredential, OAuthError

_SCOPE_SEPARATORS = {"space": " ", "comma": ",", "plus": "+"}


class UniversalOAuthProvider(BaseOAuthProvider):
    """RFC 6749 provider with configurable endpoints and vendor knobs."""

    # These get overwritten by ``__init__`` based on discovery or kwargs.
    TOKEN_ENDPOINT: str = ""
    AUTHORIZATION_ENDPOINT: str = ""
    REVOKE_ENDPOINT: str = ""
    INTROSPECTION_ENDPOINT: str = ""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        discovery_metadata: DiscoveryMetadata | None = None,
        authorization_endpoint: str | None = None,
        token_endpoint: str | None = None,
        revocation_endpoint: str | None = None,
        introspection_endpoint: str | None = None,
        scope_format: str = "space",
        scope_on_refresh: bool = False,
        requires_pkce: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Endpoints: discovery metadata first, explicit kwargs override.
        if discovery_metadata is not None:
            self.AUTHORIZATION_ENDPOINT = discovery_metadata.authorization_endpoint
            self.TOKEN_ENDPOINT = discovery_metadata.token_endpoint
            self.REVOKE_ENDPOINT = discovery_metadata.revocation_endpoint or ""
            self.INTROSPECTION_ENDPOINT = (
                discovery_metadata.introspection_endpoint or ""
            )
        if authorization_endpoint:
            self.AUTHORIZATION_ENDPOINT = authorization_endpoint
        if token_endpoint:
            self.TOKEN_ENDPOINT = token_endpoint
        if revocation_endpoint:
            self.REVOKE_ENDPOINT = revocation_endpoint
        if introspection_endpoint:
            self.INTROSPECTION_ENDPOINT = introspection_endpoint

        if not self.TOKEN_ENDPOINT or not self.AUTHORIZATION_ENDPOINT:
            raise OAuthError(
                "UniversalOAuthProvider requires token_endpoint and "
                "authorization_endpoint (via discovery_metadata or explicit kwargs)."
            )

        if scope_format not in _SCOPE_SEPARATORS:
            raise OAuthError(
                f"Unknown scope_format={scope_format!r}. "
                f"Expected one of: {sorted(_SCOPE_SEPARATORS)}"
            )
        self._scope_sep = _SCOPE_SEPARATORS[scope_format]
        self._scope_on_refresh = scope_on_refresh
        self.requires_pkce = requires_pkce

        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            http_client=http_client,
        )

    def _scope_string(self) -> str:
        return self._scope_sep.join(self.scopes)

    def get_authorization_url(
        self,
        state: str | None = None,
        *,
        extra_params: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> str:
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self._scope_string(),
        }
        if state:
            params["state"] = state
        if extra_params:
            params.update(extra_params)
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        redirect_uri = kwargs.get("redirect_uri") or self.redirect_uri
        params = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if self.client_secret:
            params["client_secret"] = self.client_secret
        code_verifier = kwargs.get("code_verifier")
        if code_verifier:
            params["code_verifier"] = code_verifier
        return params

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": credential.refresh_token or "",
        }
        if self.client_secret:
            params["client_secret"] = self.client_secret
        if self._scope_on_refresh:
            scopes = list(credential.scopes) if credential.scopes else list(self.scopes)
            params["scope"] = self._scope_sep.join(scopes)
        return params

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        if not self.REVOKE_ENDPOINT:
            return True  # RFC 7009 is optional; providers without it succeed silently
        token = credential.refresh_token or credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.REVOKE_ENDPOINT, data={"token": token}
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        """Generic validate — RFC 7662 introspection when available, else True.

        Without an introspection endpoint we cannot verify the token server-side
        in a standards-compliant way, so return True (optimistic) rather than
        calling vendor-specific endpoints. Subclasses override for vendors that
        expose a non-standard validation endpoint.
        """
        if not self.INTROSPECTION_ENDPOINT:
            return True
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.INTROSPECTION_ENDPOINT,
                    data={"token": access_token},
                    auth=(self.client_id, self.client_secret) if self.client_secret else None,
                )
                response.raise_for_status()
                body = response.json()
                return bool(body.get("active"))
            except Exception:
                return False
```

- [ ] **Step 4: Run universal tests**

```bash
pytest src/nexus/lib/oauth/tests/test_universal.py -v 2>&1 | tail -15
```

Expected: 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/oauth/universal.py src/nexus/lib/oauth/tests/test_universal.py
git commit -m "feat(lib/oauth): UniversalOAuthProvider (RFC 6749 + discovery/explicit endpoints)"
```

---

## Task 10: Move Google provider to `lib/oauth/providers/google.py` as a Universal subclass

**Files:**
- Create: `src/nexus/lib/oauth/providers/__init__.py`
- Create: `src/nexus/lib/oauth/providers/google.py`
- Modify: `src/nexus/bricks/auth/oauth/providers/google.py` → re-export
- Do not touch: `src/nexus/bricks/auth/oauth/tests/test_google_provider.py` (its imports still work via re-export)

- [ ] **Step 1: Create providers package**

Write `src/nexus/lib/oauth/providers/__init__.py`:

```python
"""Built-in OAuth provider subclasses (vendor quirk overrides on top of UniversalOAuthProvider)."""

from nexus.lib.oauth.providers.google import GoogleOAuthProvider
from nexus.lib.oauth.providers.microsoft import MicrosoftOAuthProvider
from nexus.lib.oauth.providers.slack import SlackOAuthProvider
from nexus.lib.oauth.providers.x import XOAuthProvider

__all__ = [
    "GoogleOAuthProvider",
    "MicrosoftOAuthProvider",
    "SlackOAuthProvider",
    "XOAuthProvider",
]
```

(We will create the files below in subsequent tasks; this import will fail until Task 13. That's fine — we only use this `__init__.py` once everything is in place. Skip the import failure for now by leaving this file empty and filling it in at the end of Task 13.)

Actually — to avoid an intermediate broken state, write it empty now:

```python
```

We'll populate it in Task 13's Step 5.

- [ ] **Step 2: Write `lib/oauth/providers/google.py`**

Write `src/nexus/lib/oauth/providers/google.py`:

```python
"""Google OAuth 2.0 provider.

Thin subclass of :class:`UniversalOAuthProvider` with Google-specific quirks:

- ``access_type=offline`` + ``prompt=consent`` on authorization (required for
  refresh tokens).
- Non-standard ``tokeninfo`` validation endpoint (instead of RFC 7662).
- Silent-accept on ``revoke`` failure (Google returns 200 on already-revoked
  tokens).
"""

from __future__ import annotations

from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


class GoogleOAuthProvider(UniversalOAuthProvider):
    """Google OAuth 2.0 (Drive / Gmail / Calendar / Cloud Storage)."""

    TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            revocation_endpoint="https://oauth2.googleapis.com/revoke",
            scope_format="space",
            scope_on_refresh=False,
            requires_pkce=False,
            http_client=http_client,
        )

    def get_authorization_url(
        self,
        state: str | None = None,
        redirect_uri: str | None = None,
        **kwargs: Any,
    ) -> str:
        extras = {"access_type": "offline", "prompt": "consent"}
        if redirect_uri and redirect_uri != self.redirect_uri:
            # Temporary override: save-restore to avoid mutating provider state.
            original = self.redirect_uri
            self.redirect_uri = redirect_uri
            try:
                return super().get_authorization_url(state=state, extra_params=extras, **kwargs)
            finally:
                self.redirect_uri = original
        return super().get_authorization_url(state=state, extra_params=extras, **kwargs)

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.TOKENINFO_ENDPOINT, params={"access_token": access_token}
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        # Google silently succeeds on already-revoked tokens; mimic the
        # prior behavior of returning False only on network / 4xx errors.
        token = credential.refresh_token or credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(self.REVOKE_ENDPOINT, params={"token": token})
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False
```

- [ ] **Step 3: Replace brick google.py with re-export**

Overwrite `src/nexus/bricks/auth/oauth/providers/google.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.providers.google``."""

from nexus.lib.oauth.providers.google import GoogleOAuthProvider

__all__ = ["GoogleOAuthProvider"]
```

- [ ] **Step 4: Run Google provider tests**

```bash
pytest src/nexus/bricks/auth/oauth/tests/test_google_provider.py -v 2>&1 | tail -10
```

Expected: all existing tests pass. If any test checks exact attribute names like `AUTHORIZATION_ENDPOINT`, those are inherited from `UniversalOAuthProvider` via `__init__`, so they will still be present as instance attributes (set by super().__init__).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/oauth/providers/__init__.py \
        src/nexus/lib/oauth/providers/google.py \
        src/nexus/bricks/auth/oauth/providers/google.py
git commit -m "refactor(lib/oauth): Google provider as UniversalOAuthProvider subclass"
```

---

## Task 11: Move Microsoft provider to `lib/oauth/providers/microsoft.py`

**Files:**
- Create: `src/nexus/lib/oauth/providers/microsoft.py`
- Modify: `src/nexus/bricks/auth/oauth/providers/microsoft.py` → re-export

Quirks kept:
- Auto-append `offline_access` to scopes
- `response_mode=query` on authorize URL
- No-op `revoke_token` (Microsoft has no revocation endpoint)
- `/me` Graph validation

- [ ] **Step 1: Write `lib/oauth/providers/microsoft.py`**

```python
"""Microsoft OAuth 2.0 provider (Graph / Entra ID)."""

from __future__ import annotations

from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


class MicrosoftOAuthProvider(UniversalOAuthProvider):
    """Microsoft Identity Platform (common tenant)."""

    GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        scopes_with_offline = list(scopes)
        if "offline_access" not in scopes_with_offline:
            scopes_with_offline.append("offline_access")
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes_with_offline,
            provider_name=provider_name,
            authorization_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scope_format="space",
            scope_on_refresh=True,
            requires_pkce=False,
            http_client=http_client,
        )

    def get_authorization_url(
        self, state: str | None = None, **_kwargs: Any
    ) -> str:
        return super().get_authorization_url(
            state=state, extra_params={"response_mode": "query"}
        )

    async def revoke_token(self, _credential: OAuthCredential) -> bool:
        # Microsoft has no standard revocation API; treat as success.
        return True

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    f"{self.GRAPH_ENDPOINT}/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return True
            except Exception:
                return False
```

- [ ] **Step 2: Replace brick microsoft.py with re-export**

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.providers.microsoft``."""

from nexus.lib.oauth.providers.microsoft import MicrosoftOAuthProvider

__all__ = ["MicrosoftOAuthProvider"]
```

- [ ] **Step 3: Run Microsoft tests**

```bash
pytest src/nexus/bricks/auth/oauth/tests/test_microsoft_provider.py -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/lib/oauth/providers/microsoft.py \
        src/nexus/bricks/auth/oauth/providers/microsoft.py
git commit -m "refactor(lib/oauth): Microsoft provider as UniversalOAuthProvider subclass"
```

---

## Task 12: Move X provider to `lib/oauth/providers/x.py` (use base PKCE)

**Files:**
- Create: `src/nexus/lib/oauth/providers/x.py`
- Modify: `src/nexus/bricks/auth/oauth/providers/x.py` → re-export

X keeps its unique quirks:
- Mandatory PKCE (set `requires_pkce=True`, inherited base handles the challenge/verifier)
- Basic Auth header for confidential clients on token exchange + refresh
- Lowercase `bearer` → `Bearer` normalization
- Hardcoded default scopes (backward compat)
- `POST /2/oauth2/revoke` with token-as-form
- `/users/me` validation
- `metadata` preservation on refresh

PKCE generation code (base64+sha256+rstrip) is deleted — base class handles it.

- [ ] **Step 1: Write `lib/oauth/providers/x.py`**

```python
"""X (Twitter) OAuth 2.0 provider with mandatory PKCE."""

from __future__ import annotations

import base64
import dataclasses
from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


_DEFAULT_SCOPES = [
    "tweet.read",
    "tweet.write",
    "tweet.moderate.write",
    "users.read",
    "follows.read",
    "offline.access",
    "bookmark.read",
    "bookmark.write",
    "list.read",
    "like.read",
    "like.write",
]


class XOAuthProvider(UniversalOAuthProvider):
    """X (Twitter) OAuth 2.0 with PKCE.

    ``client_secret`` is optional for public clients; when set, Basic Auth is
    used on the token endpoint.
    """

    USERS_ME_ENDPOINT = "https://api.twitter.com/2/users/me"

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
        provider_name: str = "x",
        client_secret: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret or "",
            redirect_uri=redirect_uri,
            scopes=scopes or _DEFAULT_SCOPES,
            provider_name=provider_name,
            authorization_endpoint="https://twitter.com/i/oauth2/authorize",
            token_endpoint="https://api.twitter.com/2/oauth2/token",
            revocation_endpoint="https://api.twitter.com/2/oauth2/revoke",
            scope_format="space",
            scope_on_refresh=False,
            requires_pkce=True,
            http_client=http_client,
        )

    # Re-expose the PKCE helper on this class for backward compatibility with
    # the old x.py ``get_authorization_url_with_pkce`` call sites in
    # ``nexus.fs._oauth_support``.
    # Base class already provides this method; no override needed.

    def _basic_auth_header(self) -> dict[str, str] | None:
        if not self.client_secret:
            return None
        cred = f"{self.client_id}:{self.client_secret}".encode()
        encoded = base64.b64encode(cred).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def _build_exchange_headers(self) -> dict[str, str] | None:
        headers = self._basic_auth_header()
        if headers is None:
            headers = {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _build_refresh_headers(self) -> dict[str, str] | None:
        headers = self._basic_auth_header() or {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        cred = super()._parse_token_response(token_data)
        if cred.token_type.lower() == "bearer":
            cred = dataclasses.replace(cred, token_type="Bearer")
        return cred

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        new_cred = await super().refresh_token(credential)
        # Preserve metadata across refresh (X-specific).
        if credential.metadata:
            new_cred = dataclasses.replace(new_cred, metadata=credential.metadata)
        if new_cred.token_type.lower() == "bearer":
            new_cred = dataclasses.replace(new_cred, token_type="Bearer")
        return new_cred

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        token = credential.access_token
        if not token:
            return False
        data: dict[str, str] = {
            "token": token,
            "token_type_hint": "access_token",
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.REVOKE_ENDPOINT,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.USERS_ME_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return True
            except Exception:
                return False
```

- [ ] **Step 2: Replace brick x.py with re-export**

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.providers.x``."""

from nexus.lib.oauth.providers.x import XOAuthProvider

__all__ = ["XOAuthProvider"]
```

- [ ] **Step 3: Run X provider tests**

```bash
pytest src/nexus/bricks/auth/oauth/tests/test_x_provider.py -v 2>&1 | tail -15
```

Expected: all pass. If any test references `XOAuthProvider.DEFAULT_SCOPES` or the old pre-PKCE `exchange_code` stub raise, update the test to call `exchange_code_pkce()` instead.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/lib/oauth/providers/x.py src/nexus/bricks/auth/oauth/providers/x.py
git commit -m "refactor(lib/oauth): X provider as UniversalOAuthProvider subclass (base PKCE)"
```

---

## Task 13: Implement `SlackOAuthProvider` in `lib/oauth/providers/slack.py` (TDD)

**Files:**
- Create: `src/nexus/lib/oauth/providers/slack.py`
- Create: `src/nexus/bricks/auth/oauth/providers/slack.py` (re-export)
- Test: `src/nexus/lib/oauth/tests/test_slack_provider.py`
- Modify: `src/nexus/lib/oauth/providers/__init__.py` (populate exports)

Slack v2 OAuth differs from the other three in a few concrete ways:
- Authorize URL: `https://slack.com/oauth/v2/authorize`
- Token URL: `https://slack.com/api/oauth.v2.access`
- Scope separator: **comma**, not space.
- Two kinds of scopes: bot scopes (`scope`) and user scopes (`user_scope`) — we will use bot scopes only for now.
- Token response wraps access_token inside a non-standard shape: `{"ok": true, "access_token": "...", "token_type": "bot", "scope": "...", "team": {...}, "authed_user": {...}}`. No `expires_in` on bot tokens (they don't expire). No standard `refresh_token`.
- Slack's revoke is `POST https://slack.com/api/auth.revoke` with the token in Authorization header.

- [ ] **Step 1: Write failing test**

Write `src/nexus/lib/oauth/tests/test_slack_provider.py`:

```python
"""Tests for Slack OAuth v2 provider."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.lib.oauth.providers.slack import SlackOAuthProvider


def _provider(
    scopes: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> SlackOAuthProvider:
    return SlackOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=scopes or ["channels:read", "chat:write"],
        provider_name="slack",
        http_client=http_client,
    )


def test_authorize_url_uses_comma_scopes() -> None:
    url = _provider().get_authorization_url(state="abc")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["channels:read,chat:write"]
    assert q["state"] == ["abc"]
    assert "slack.com/oauth/v2/authorize" in url


@pytest.mark.asyncio
async def test_exchange_code_parses_v2_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/oauth.v2.access"
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert body["code"] == "code123"
        assert body["client_id"] == "cid"
        assert body["client_secret"] == "secret"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-bot-token",
                "token_type": "bot",
                "scope": "channels:read,chat:write",
                "team": {"id": "T1", "name": "Acme"},
                "authed_user": {"id": "U1"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cred = await _provider(http_client=client).exchange_code("code123")

    assert cred.access_token == "xoxb-bot-token"
    assert cred.token_type == "bot"
    assert cred.scopes == ("channels:read", "chat:write")
    assert cred.metadata is not None
    assert cred.metadata["team_id"] == "T1"
    assert cred.metadata["team_name"] == "Acme"
    assert cred.metadata["authed_user_id"] == "U1"


@pytest.mark.asyncio
async def test_exchange_code_raises_on_ok_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(Exception) as exc_info:
            await _provider(http_client=client).exchange_code("bad")
    assert "invalid_code" in str(exc_info.value)


@pytest.mark.asyncio
async def test_revoke_token_posts_to_auth_revoke() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth.revoke"
        assert request.headers.get("Authorization") == "Bearer xoxb-token"
        return httpx.Response(200, json={"ok": True, "revoked": True})

    from nexus.lib.oauth.types import OAuthCredential

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = _provider(http_client=client)
        cred = OAuthCredential(access_token="xoxb-token", provider="slack")
        assert await provider.revoke_token(cred) is True
```

- [ ] **Step 2: Run test (expect failure)**

```bash
pytest src/nexus/lib/oauth/tests/test_slack_provider.py -v 2>&1 | tail -15
```

Expected: ImportError.

- [ ] **Step 3: Implement `slack.py`**

Write `src/nexus/lib/oauth/providers/slack.py`:

```python
"""Slack OAuth v2 provider (bot-token flow)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential, OAuthError
from nexus.lib.oauth.universal import UniversalOAuthProvider


class SlackOAuthProvider(UniversalOAuthProvider):
    """Slack OAuth v2 (bot token).

    Slack deviates from RFC 6749 in two ways that matter here:

    - Scopes are comma-separated on the authorize URL (RFC 6749 says space).
    - The token response is wrapped in a ``{"ok": bool, ...}`` envelope and
      doesn't follow the ``access_token`` / ``expires_in`` convention for bot
      tokens (which don't expire). User-scope refresh tokens exist but are
      not handled here — add ``scope_on_refresh=True`` + a user-token subclass
      if/when that is needed.
    """

    REVOKE_ENDPOINT = "https://slack.com/api/auth.revoke"
    AUTH_TEST_ENDPOINT = "https://slack.com/api/auth.test"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str = "slack",
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            authorization_endpoint="https://slack.com/oauth/v2/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.access",
            revocation_endpoint=self.REVOKE_ENDPOINT,
            scope_format="comma",
            scope_on_refresh=False,
            requires_pkce=False,
            http_client=http_client,
        )

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        if not token_data.get("ok", False):
            raise OAuthError(
                f"Slack OAuth error: {token_data.get('error', 'unknown')}"
            )

        scope_str: str = token_data.get("scope", "")
        scopes = tuple(s for s in scope_str.split(",") if s) or None

        expires_at = None
        if "expires_in" in token_data:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=int(token_data["expires_in"])
            )

        metadata: dict[str, Any] = {}
        team = token_data.get("team") or {}
        if team.get("id"):
            metadata["team_id"] = team["id"]
        if team.get("name"):
            metadata["team_name"] = team["name"]
        authed_user = token_data.get("authed_user") or {}
        if authed_user.get("id"):
            metadata["authed_user_id"] = authed_user["id"]

        return OAuthCredential(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "bot"),
            expires_at=expires_at,
            scopes=scopes,
            provider=self.provider_name,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
            metadata=metadata or None,
        )

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        token = credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.REVOKE_ENDPOINT,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()
                return bool(data.get("ok"))
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.AUTH_TEST_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return bool(response.json().get("ok"))
            except Exception:
                return False
```

- [ ] **Step 4: Run Slack tests**

```bash
pytest src/nexus/lib/oauth/tests/test_slack_provider.py -v 2>&1 | tail -15
```

Expected: 4 tests pass.

- [ ] **Step 5: Populate `lib/oauth/providers/__init__.py`**

Overwrite `src/nexus/lib/oauth/providers/__init__.py` with:

```python
"""Built-in OAuth provider subclasses (vendor quirks on UniversalOAuthProvider)."""

from nexus.lib.oauth.providers.google import GoogleOAuthProvider
from nexus.lib.oauth.providers.microsoft import MicrosoftOAuthProvider
from nexus.lib.oauth.providers.slack import SlackOAuthProvider
from nexus.lib.oauth.providers.x import XOAuthProvider

__all__ = [
    "GoogleOAuthProvider",
    "MicrosoftOAuthProvider",
    "SlackOAuthProvider",
    "XOAuthProvider",
]
```

- [ ] **Step 6: Write brick re-export for Slack**

Write `src/nexus/bricks/auth/oauth/providers/slack.py`:

```python
"""Compat shim — canonical location is ``nexus.lib.oauth.providers.slack``."""

from nexus.lib.oauth.providers.slack import SlackOAuthProvider

__all__ = ["SlackOAuthProvider"]
```

- [ ] **Step 7: Commit**

```bash
git add src/nexus/lib/oauth/providers/slack.py \
        src/nexus/lib/oauth/providers/__init__.py \
        src/nexus/lib/oauth/tests/test_slack_provider.py \
        src/nexus/bricks/auth/oauth/providers/slack.py
git commit -m "feat(lib/oauth): add Slack OAuth v2 provider (bot token)"
```

---

## Task 14: Fix `configs/oauth.yaml` class paths + point to lib

**Files:**
- Modify: `configs/oauth.yaml:77, 91, 113` + add Slack line-by-line

Before:
```yaml
# line 77
provider_class: nexus.server.auth.microsoft_oauth.MicrosoftOAuthProvider
# line 91
provider_class: nexus.server.auth.x_oauth.XOAuthProvider
# line 113
provider_class: nexus.server.auth.slack_oauth.SlackOAuthProvider
```

After (point at the canonical lib paths; brick re-exports mean old paths would also work, but the lib path is the stable one going forward):

- [ ] **Step 1: Update the three paths**

Edit `configs/oauth.yaml`:

```yaml
# line 77
provider_class: nexus.lib.oauth.providers.microsoft.MicrosoftOAuthProvider

# line 91
provider_class: nexus.lib.oauth.providers.x.XOAuthProvider

# line 113
provider_class: nexus.lib.oauth.providers.slack.SlackOAuthProvider
```

Also update the Google entries (lines 17, 32, 47, 62) from
`nexus.bricks.auth.oauth.providers.google.GoogleOAuthProvider` to
`nexus.lib.oauth.providers.google.GoogleOAuthProvider` for consistency.

- [ ] **Step 2: Verify the factory can load every provider now**

```bash
python3 <<'PY'
import sys
sys.path.insert(0, "src")
import os
os.environ["NEXUS_OAUTH_GOOGLE_CLIENT_ID"] = "x"
os.environ["NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"] = "x"
os.environ["NEXUS_OAUTH_MICROSOFT_CLIENT_ID"] = "x"
os.environ["NEXUS_OAUTH_MICROSOFT_CLIENT_SECRET"] = "x"
os.environ["NEXUS_OAUTH_X_CLIENT_ID"] = "x"
os.environ["NEXUS_OAUTH_X_CLIENT_SECRET"] = "x"
os.environ["NEXUS_OAUTH_SLACK_CLIENT_ID"] = "x"
os.environ["NEXUS_OAUTH_SLACK_CLIENT_SECRET"] = "x"
from nexus.bricks.auth.oauth.factory import OAuthProviderFactory
factory = OAuthProviderFactory.from_file("configs/oauth.yaml")
for name in ("google-drive", "gmail", "gcalendar", "microsoft-onedrive", "x", "slack"):
    provider = factory.create_provider(name=name)
    print(f"{name}: {type(provider).__name__} OK")
PY
```

Expected: every provider prints `… OK`. Previously Microsoft/X/Slack raised AttributeError.

- [ ] **Step 3: Commit**

```bash
git add configs/oauth.yaml
git commit -m "fix(oauth): oauth.yaml provider_class paths point to canonical lib/oauth"
```

---

## Task 15: Update `nexus.fs._oauth_support` to import providers from `lib/oauth`

**Files:**
- Modify: `src/nexus/fs/_oauth_support.py:108-113` (lazy import targets)

Today `_get_token_manager_cls()` lazy-imports from `nexus.bricks.auth.oauth.token_manager` and `_get_x_oauth_provider_cls()` from `nexus.bricks.auth.oauth.providers.x`. Provider lazy-imports should now target `nexus.lib.oauth.providers.x` so a pure-slim install can resolve them.

`TokenManager` continues to live in bricks (full-only), so slim-only OAuth still requires the full package. That's a known limitation outside this plan's scope (token storage lives in bricks because it depends on the auth-store protocols).

- [ ] **Step 1: Repoint X provider lazy import**

Edit `src/nexus/fs/_oauth_support.py`, line 112-113:

```python
def _get_x_oauth_provider_cls() -> Any:
    return _il.import_module("nexus.lib.oauth.providers.x").XOAuthProvider
```

- [ ] **Step 2: Repoint Google provider lazy imports**

Find each occurrence of `_il.import_module("nexus.bricks.auth.oauth.providers.google")` and replace with `_il.import_module("nexus.lib.oauth.providers.google")`. Occurrences are at lines 233, 330, 503 (approximately — use `rtk grep -n "nexus.bricks.auth.oauth.providers.google" src/nexus/fs/_oauth_support.py` to verify).

- [ ] **Step 3: Verify slim module still imports**

```bash
python3 -c "import sys; sys.path.insert(0,'src'); import nexus.fs._oauth_support; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Re-run end-to-end smoke from Task 14 Step 2**

Expected: every provider prints `… OK`.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fs/_oauth_support.py
git commit -m "refactor(fs): _oauth_support lazy-imports providers from lib/oauth"
```

---

## Task 16: Final validation + PR description update

- [ ] **Step 1: Run the full OAuth-related test surface**

```bash
pytest src/nexus/bricks/auth/oauth/tests \
       src/nexus/lib/oauth/tests \
       tests/unit/fs/test_oauth_support.py \
       tests/unit/auth \
       -q 2>&1 | tail -10
```

Expected: 0 failures, pass count ≥ baseline from Task 1 plus the new tests we added (4 pkce + 5 discovery + 7 universal + 2 base_pkce + 4 slack = **22 new tests**).

- [ ] **Step 2: Build slim wheel locally and verify imports work without full**

```bash
cd packages/nexus-fs
ln -sf ../../src src
sed -i.bak 's|"../../src/nexus"|"src/nexus"|' pyproject.toml
python3 -m hatchling build -d /tmp/dist-nexus-fs
git checkout pyproject.toml
rm -f src pyproject.toml.bak
cd ../..

python3 -m venv /tmp/slim-test
/tmp/slim-test/bin/pip install /tmp/dist-nexus-fs/*.whl
/tmp/slim-test/bin/python -c "
from nexus.lib.oauth import UniversalOAuthProvider, DiscoveryClient, generate_pkce_pair
from nexus.lib.oauth.providers import GoogleOAuthProvider, SlackOAuthProvider
print('slim lib/oauth OK')
"
```

Expected: `slim lib/oauth OK`.

- [ ] **Step 3: Push to PR #3815**

```bash
git log --oneline origin/worktree-atomic-singing-cook..HEAD | head -30
git push origin worktree-atomic-singing-cook
```

Expected: push succeeds; PR #3815 now shows all new commits.

- [ ] **Step 4: Update PR #3815 description**

```bash
gh pr edit 3815 --title "feat(oauth): universal OAuth in lib/ + Slack provider + fix 3 oauth.yaml class-path bugs" --body "$(cat <<'EOF'
## Summary
Lifts OAuth primitives to ``src/nexus/lib/oauth/`` so both ``nexus-fs`` slim and ``nexus-ai-fs`` full wheels ship identical OAuth code. Adds RFC 8414 discovery, PKCE in the base class, a working Slack provider, and fixes three broken provider_class paths that prevented Microsoft/X/Slack from being instantiated from oauth.yaml. Also ships the Issue #3815 original release bits (nexus-fs 0.4.8 bump, OAuth factory dev-path fix, NEXUS_STATE_DIR lazy resolution).

## Changes
- ``src/nexus/lib/oauth/`` — new package (base, types, protocol, crypto, pkce, discovery, universal, providers).
- Brick-side OAuth files become compat re-exports.
- Added ``SlackOAuthProvider`` (was referenced in oauth.yaml but never implemented).
- Added RFC 8414 / OIDC Discovery client.
- Added PKCE support to BaseOAuthProvider (was duplicated inline in X only).
- ``nexus-fs`` slim base deps gain ``httpx>=0.28``.
- ``configs/oauth.yaml`` provider_class paths fixed and repointed to the canonical ``nexus.lib.oauth.providers.*`` location.

## Release plan
After merge: tag ``v0.9.31`` + ``nexus-fs-v0.4.8`` on develop to trigger the release workflow (covers nexus-ai-fs, nexus-fs, nexus-tui, nexus-api-client, nexus-kernel).

## Test plan
- [x] New tests: PKCE (4), discovery (5), universal (7), base-PKCE (2), Slack (4) = 22 added.
- [x] All existing brick OAuth tests still pass via the re-export shims.
- [x] Pure slim wheel smoke: ``import nexus.lib.oauth`` + ``GoogleOAuthProvider`` / ``SlackOAuthProvider`` resolve without the bricks tree.
- [x] Factory instantiates every provider named in oauth.yaml (Microsoft/X/Slack were broken before).
- [ ] CI green on develop branch rules.
EOF
)"
```

- [ ] **Step 5: Commit only if there are doc changes**

If there are any lingering unstaged files (plan doc, generated artifacts), stage or leave them out explicitly — do not bundle them into release commits.

---

## Self-Review Notes

**Spec coverage:**

| Requirement from brainstorm | Task |
|---|---|
| Lift shared primitives to `lib/oauth/` | 3, 4, 5, 7 |
| PKCE in base class (extract from X) | 6, 7, 12 |
| RFC 8414 discovery | 8 |
| UniversalOAuthProvider | 9 |
| Quirk-subclass refactor of existing providers | 10, 11, 12 |
| Implement SlackOAuthProvider | 13 |
| Fix oauth.yaml class paths | 14 |
| Slim supports universal OAuth without bricks co-install | 2 (httpx), 15 |
| httpx in slim base deps | 2 |
| One big PR on current branch | End-to-end |

**Placeholder scan:** None — every code step includes full text. Every commit has its message. Expected outputs are stated.

**Type consistency check:**
- `DiscoveryMetadata` fields match between definition (Task 8) and usage (Task 9 `UniversalOAuthProvider.__init__`).
- `generate_pkce_pair()` returns `tuple[str, str]` in Task 6; used in Task 7's base PKCE method same shape.
- `OAuthCredential.metadata` mutated via `dataclasses.replace` in X refresh (Task 12) — `metadata` field has `hash=False, compare=False` per existing types.py, so replace works.
- `scope_format` values accepted: `"space" | "comma" | "plus"` — Slack uses `"comma"` (Task 13), Google/Microsoft/X use `"space"` (Tasks 10/11/12).

**Risk notes:**
- Task 10 Step 4 — if existing google_provider tests check `GoogleOAuthProvider.AUTHORIZATION_ENDPOINT` as a **class** attribute (not instance), tests may need a minor update since `UniversalOAuthProvider` sets endpoints via `__init__` not on the class. Acceptable: one-line test fixture update.
- Task 14 Step 2 — requires client_id/secret envs set; otherwise factory raises. The inline env setup covers this.
- The slim wheel smoke (Task 16 Step 2) won't have `cachetools` available, so `pending.py` can't be imported. The smoke imports only `lib/oauth` and `lib/oauth.providers` which never reach `pending.py`. This is intentional.
