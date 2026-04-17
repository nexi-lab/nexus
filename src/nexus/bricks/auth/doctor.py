"""Auth doctor — unified health check across all auth sources.

Emits per-source labeled output with actionable fix hints.  Fix hints are
driven by a single table (FIX_HINTS) keyed by (failure_reason, provider) —
anything requiring a fix hint must add its entry here, never scattered across
call sites.  See issue #3741 / decision 11A.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nexus.bricks.auth.profile import AuthProfileFailureReason

# Table of fix hints.  Key: (failure_reason, provider).  Provider=None is the
# generic fallback.  Every value of AuthProfileFailureReason MUST have a
# generic entry (the coverage test enforces this).
FIX_HINTS: dict[tuple[AuthProfileFailureReason, str | None], str] = {
    # ------------------------------------------------------------------ #
    # Generic fallbacks — one per enum value.                             #
    # ------------------------------------------------------------------ #
    (AuthProfileFailureReason.AUTH, None): (
        "Credentials rejected. Run `nexus auth connect <service>` to re-authenticate."
    ),
    (AuthProfileFailureReason.AUTH_PERMANENT, None): (
        "Authentication failed permanently (revoked or removed upstream). "
        "Run `nexus auth disconnect <service>` then `nexus auth connect <service>`."
    ),
    (AuthProfileFailureReason.FORMAT, None): (
        "Credential format is invalid. Run `nexus auth connect <service>` to re-enter credentials."
    ),
    (AuthProfileFailureReason.OVERLOADED, None): (
        "Upstream service is overloaded. Retry in a few minutes; no action needed on your end."
    ),
    (AuthProfileFailureReason.RATE_LIMIT, None): (
        "Rate-limited by upstream. Cooldown will expire automatically; "
        "if persistent, check `nexus auth pool status` for multi-account failover options."
    ),
    (AuthProfileFailureReason.BILLING, None): (
        "Billing or quota issue upstream. Verify the account's billing status in the provider console."
    ),
    (AuthProfileFailureReason.TIMEOUT, None): (
        "Upstream timed out. Retry; if persistent, check network connectivity."
    ),
    (AuthProfileFailureReason.SESSION_EXPIRED, None): (
        "OAuth session expired. Run `nexus auth connect <service>` to refresh the session."
    ),
    (AuthProfileFailureReason.MFA_REQUIRED, None): (
        "MFA challenge required. Run `nexus auth connect <service>` interactively to complete MFA."
    ),
    (AuthProfileFailureReason.PROXY_OR_TLS, None): (
        "Network or TLS issue reaching upstream. Check HTTPS_PROXY / SSL_CERT_FILE env vars."
    ),
    (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, None): (
        "Required upstream CLI is not installed. Install it and re-run `nexus auth doctor`."
    ),
    (AuthProfileFailureReason.SCOPE_INSUFFICIENT, None): (
        "OAuth scopes are insufficient. Run `nexus auth connect <service>` to grant additional scopes."
    ),
    (AuthProfileFailureReason.CLOCK_SKEW, None): (
        "System clock is skewed — token validation fails. "
        "Sync system time (NTP) and retry: `sudo ntpdate -u time.apple.com` (macOS) "
        "or `sudo chronyc -a makestep` (Linux)."
    ),
    (AuthProfileFailureReason.UNKNOWN, None): (
        "Unclassified failure. Check `nexus auth pool status` and logs for details."
    ),
    # ------------------------------------------------------------------ #
    # Provider-specific overrides.                                         #
    # ------------------------------------------------------------------ #
    # UPSTREAM_CLI_MISSING — name the exact tool to install.
    (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "gcs"): (
        "gcloud CLI not found. Install from https://cloud.google.com/sdk/docs/install "
        "then run `gcloud auth application-default login`."
    ),
    (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "s3"): (
        "aws CLI not found. Install with `brew install awscli` or see "
        "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html."
    ),
    (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "gmail"): (
        "gws CLI not found. Install from your internal package registry, "
        "then run `nexus auth connect gmail oauth`."
    ),
    (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "gws"): (
        "gws CLI not found. Install from your internal package registry, "
        "then run `nexus auth connect gws oauth`."
    ),
    # SESSION_EXPIRED — provider-specific auth connect command.
    (AuthProfileFailureReason.SESSION_EXPIRED, "gmail"): (
        "Gmail session expired. Run `nexus-fs auth connect gmail oauth --user-email <email>` "
        "to refresh the session."
    ),
    (AuthProfileFailureReason.SESSION_EXPIRED, "gws"): (
        "Google Workspace session expired. Run `nexus-fs auth connect gws oauth --user-email <email>` "
        "to refresh the session."
    ),
    (AuthProfileFailureReason.SESSION_EXPIRED, "google-calendar"): (
        "Calendar session expired. Run `nexus-fs auth connect google-calendar oauth --user-email <email>` "
        "to refresh the session."
    ),
    (AuthProfileFailureReason.SESSION_EXPIRED, "google-drive"): (
        "Drive session expired. Run `nexus-fs auth connect google-drive oauth --user-email <email>` "
        "to refresh the session."
    ),
    # SCOPE_INSUFFICIENT — guide to reconnect with broader scopes.
    (AuthProfileFailureReason.SCOPE_INSUFFICIENT, "gmail"): (
        "Gmail scopes insufficient. Run `nexus-fs auth connect gmail oauth --user-email <email>` "
        "and approve all requested access."
    ),
    (AuthProfileFailureReason.SCOPE_INSUFFICIENT, "gws"): (
        "Google Workspace scopes insufficient. Run `nexus-fs auth connect gws oauth --user-email <email>` "
        "and approve all requested access."
    ),
}


def fix_hint_for(
    *,
    reason: AuthProfileFailureReason,
    provider: str | None,
) -> str:
    """Return the most specific fix hint for (reason, provider).

    Resolves (reason, provider) first, then falls back to (reason, None).
    Raises KeyError if no hint exists — enforced by the coverage test.
    """
    specific = FIX_HINTS.get((reason, provider))
    if specific:
        return specific
    return FIX_HINTS[(reason, None)]


@dataclass(frozen=True, slots=True)
class DoctorLine:
    """One line of `auth doctor` output."""

    source: str  # e.g. "nexus-oauth", "aws-cli", "gcloud", "gws-cli", "codex"
    service: str  # e.g. "openai", "s3", "gmail"
    profile_id: str  # e.g. "team", "default", "you@example.com"
    status: str  # "ok", "warning", "error", "cooldown"
    detail: str  # human-readable detail
    fix_hint: str = ""  # empty when status == "ok"

    def format(self) -> str:
        """Render as one line: `[source] service/profile: status — detail [— fix_hint]`."""
        head = f"[{self.source}] {self.service}/{self.profile_id}: {self.status}"
        body = f" — {self.detail}" if self.detail else ""
        hint = f" — {self.fix_hint}" if self.fix_hint else ""
        return f"{head}{body}{hint}"


def run_doctor(service: Any) -> int:
    """Invoke the unified doctor check.

    Prints one labeled line per auth profile and returns an exit code
    (0 if all ok, 1 if any failure / cooldown).
    """
    import click

    lines = list(_collect_lines(service))
    any_failures = False
    for line in lines:
        click.echo(line.format())
        if line.status in {"error", "cooldown"}:
            any_failures = True
    return 1 if any_failures else 0


def _collect_lines(service: Any) -> Iterable[DoctorLine]:
    """Walk the unified service's summaries, emit DoctorLine instances.

    AuthSummary (nexus.contracts.unified_auth) does not carry profile_id or
    failure_reason yet — those fields arrive in Phase 4's profile store.  We
    derive the best available values from the fields that do exist:

    - source: summary.source (e.g. "oauth", "native", "missing") — used as
      both the source label and profile_id fallback when no richer ID exists.
    - failure_reason: not present on AuthSummary today, so fix_hint is omitted
      until the profile store is wired (Task 12 / Phase B).
    """

    # list_summaries() is async — run it synchronously in this sync context.
    summaries = asyncio.run(service.list_summaries())

    for summary in summaries:
        reason = getattr(summary, "failure_reason", None)
        provider = summary.service
        status = _status_from_summary(summary)
        fix = ""
        if reason is not None and status != "ok":
            fix = fix_hint_for(reason=reason, provider=provider)

        # Derive a readable source label from summary.source.
        # summary.source values: "oauth", "native", "native:gws_cli",
        # "stored:secret", "missing", "explicit_config", etc.
        raw_source = getattr(summary, "source", None) or "nexus-oauth"
        source_label = _source_label(raw_source)

        # profile_id: AuthSummary has no profile_id field yet.  Use the source
        # as a placeholder so the output is still informative.
        profile_id = getattr(summary, "profile_id", None) or raw_source

        yield DoctorLine(
            source=source_label,
            service=provider,
            profile_id=profile_id,
            status=status,
            detail=summary.message,
            fix_hint=fix,
        )


def _source_label(source: str) -> str:
    """Derive a short doctor source label from summary.source."""
    if source.startswith("native:gws_cli") or source == "native:gws_cli":
        return "gws-cli"
    if "gcloud" in source:
        return "gcloud"
    if source.startswith("stored:"):
        # e.g. "stored:secret" → keep as-is so the label is informative
        return source
    if source in {"oauth", "nexus-oauth"}:
        return "nexus-oauth"
    if source == "native":
        return "native"
    if source == "missing":
        return "nexus-oauth"
    return source


def _status_from_summary(summary: Any) -> str:
    """Map summary.status (AuthStatus) -> doctor status token."""
    from nexus.contracts.unified_auth import AuthStatus

    status = summary.status
    if status == AuthStatus.AUTHED:
        return "ok"
    if status == AuthStatus.UNKNOWN:
        return "warning"
    if status == AuthStatus.EXPIRED:
        return "error"
    if status == AuthStatus.NO_AUTH:
        return "error"
    # AuthStatus.ERROR and any future values
    return "error"
