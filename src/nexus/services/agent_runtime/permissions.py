"""§3 Permission System — CC-like rule-based tool permission matching.

Three-checkpoint pipeline (CC: toolExecution.ts:683-929):
    1. validateInput — input shape validation, blocked patterns
    2. PreToolUse hooks — user-defined hook chain (config-driven)
    3. checkPermissions — rule-based permission matching

V1: rule-based deny/allow (Python, Rust acceleration follow-up).
V2: add interactive terminal prompt (Ask action).

References:
    - CC: toolExecution.ts:683-929
    - CC: bashPermissions.ts — wildcard pattern matching
    - CC: bashSecurity.ts:77-101 — 23 security check categories
"""

from __future__ import annotations

import fnmatch
import logging
import re
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission result types
# ---------------------------------------------------------------------------


class PermissionAction(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # V2: interactive prompt


class PermissionResult:
    """Result of a permission check."""

    __slots__ = ("allowed", "reason", "rule_name")

    def __init__(self, *, allowed: bool, reason: str = "", rule_name: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        self.rule_name = rule_name


# ---------------------------------------------------------------------------
# §3.1 PermissionService Protocol
# ---------------------------------------------------------------------------


class PermissionService(Protocol):
    """Pluggable tool permission checker."""

    def check(self, tool_name: str, args: dict[str, Any]) -> PermissionResult:
        """Check if a tool call is allowed. Called before tool.call()."""
        ...


# ---------------------------------------------------------------------------
# §3.1 RuleBasedPermissionService — CC-like wildcard matching
# ---------------------------------------------------------------------------


class PermissionRule:
    """Single permission rule with tool pattern + action.

    Pattern syntax (CC-compatible):
        "Bash(git *)"      → tool=Bash, arg pattern="git *"
        "FileWrite(/etc/*)" → tool=FileWrite, arg pattern="/etc/*"
        "Bash"              → tool=Bash, any args
        "*"                 → any tool, any args
    """

    __slots__ = ("name", "tool_pattern", "arg_pattern", "action", "reason")

    def __init__(
        self,
        *,
        name: str = "",
        tool_pattern: str = "*",
        arg_pattern: str | None = None,
        action: PermissionAction = PermissionAction.ALLOW,
        reason: str = "",
    ) -> None:
        self.name = name
        self.tool_pattern = tool_pattern
        self.arg_pattern = arg_pattern
        self.action = action
        self.reason = reason

    def matches(self, tool_name: str, args: dict[str, Any]) -> bool:
        """Check if this rule matches the given tool call."""
        if not fnmatch.fnmatch(tool_name, self.tool_pattern):
            return False
        if self.arg_pattern is None:
            return True
        # Match arg pattern against first string argument (command for Bash, path for File*)
        arg_str = _extract_primary_arg(tool_name, args)
        return fnmatch.fnmatch(arg_str, self.arg_pattern)

    @classmethod
    def from_config(cls, entry: dict[str, Any]) -> "PermissionRule":
        """Parse a rule from config dict.

        Config format:
            tool_pattern: "Bash(git push *)"
            action: "deny"
            reason: "No direct push to remote"
        """
        raw_pattern = entry.get("tool_pattern", "*")
        action_str = entry.get("action", "allow").lower()
        action = PermissionAction(action_str)

        # Parse "Tool(arg_pattern)" syntax
        m = re.match(r"^(\w+)\((.+)\)$", raw_pattern)
        if m:
            tool_pattern = m.group(1)
            arg_pattern = m.group(2)
        else:
            tool_pattern = raw_pattern
            arg_pattern = None

        return cls(
            name=entry.get("name", raw_pattern),
            tool_pattern=tool_pattern,
            arg_pattern=arg_pattern,
            action=action,
            reason=entry.get("reason", ""),
        )


class RuleBasedPermissionService:
    """CC-like rule-based permission matcher.

    Rules are evaluated in order. First match wins. Default: allow.
    CC: bashPermissions.ts wildcard pattern matching.
    """

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules = rules or []

    def check(self, tool_name: str, args: dict[str, Any]) -> PermissionResult:
        for rule in self._rules:
            if rule.matches(tool_name, args):
                if rule.action == PermissionAction.DENY:
                    return PermissionResult(
                        allowed=False,
                        reason=rule.reason or f"Denied by rule: {rule.name}",
                        rule_name=rule.name,
                    )
                if rule.action == PermissionAction.ALLOW:
                    return PermissionResult(allowed=True, rule_name=rule.name)
                # ASK → V2, treat as deny for now
                return PermissionResult(
                    allowed=False,
                    reason=f"Permission requires approval (rule: {rule.name})",
                    rule_name=rule.name,
                )

        # Default: allow (no matching rule)
        return PermissionResult(allowed=True)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RuleBasedPermissionService":
        """Load from agent config.

        Config path: settings.agent.permissions.rules
        """
        rules_config = (
            config.get("settings", {}).get("agent", {}).get("permissions", {}).get("rules", [])
        )
        rules = [PermissionRule.from_config(r) for r in rules_config]
        return cls(rules)


# ---------------------------------------------------------------------------
# §3.3 BashCommandValidator — 23-category security checks
# ---------------------------------------------------------------------------

# CC: bashSecurity.ts:77-101. Each category has pattern + error message.
_BASH_SECURITY_CHECKS: list[tuple[str, re.Pattern[str], str]] = [
    # 1. Command substitution
    ("command_substitution", re.compile(r"\$\("), "Command substitution $() is not allowed"),
    ("backtick_substitution", re.compile(r"`"), "Backtick command substitution is not allowed"),
    # 2. Process substitution
    ("process_substitution", re.compile(r"<\(|>\("), "Process substitution is not allowed"),
    # 3. Shell metacharacters (pipe to dangerous commands)
    (
        "pipe_to_shell",
        re.compile(r"\|\s*(bash|sh|zsh|dash|ksh|csh|tcsh|fish)\b"),
        "Piping to shell interpreter is not allowed",
    ),
    # 4. /proc/environ access
    (
        "proc_environ",
        re.compile(r"/proc/[^/]*/environ|/proc/self/environ"),
        "Reading /proc/environ is not allowed",
    ),
    # 5. curl/wget to shell
    (
        "curl_to_shell",
        re.compile(r"(curl|wget)\s.*\|\s*(bash|sh|zsh)"),
        "Downloading and piping to shell is not allowed",
    ),
    # 6. eval/exec
    ("eval_exec", re.compile(r"\beval\b|\bexec\b"), "eval/exec is not allowed"),
    # 7. Environment variable manipulation
    (
        "env_manipulation",
        re.compile(r"\bexport\s+LD_|DYLD_|LD_PRELOAD|LD_LIBRARY_PATH"),
        "Modifying linker environment variables is not allowed",
    ),
    # 8. History file access
    (
        "history_access",
        re.compile(r"~/\..*_history|\$HISTFILE|\.bash_history|\.zsh_history"),
        "Accessing shell history files is not allowed",
    ),
    # 9. Disk/filesystem destructive operations
    (
        "disk_destructive",
        re.compile(r"\bmkfs\b|\bdd\s+if=|fdisk|parted|wipefs"),
        "Disk/filesystem destructive operations are not allowed",
    ),
    # 10. Network listeners
    (
        "network_listener",
        re.compile(r"\bnc\s+-l|\bncat\s+-l|\bsocat\b.*LISTEN"),
        "Starting network listeners is not allowed",
    ),
    # 11. Cron/at job creation
    (
        "cron_at",
        re.compile(r"\bcrontab\s+-e|\bcrontab\s+-r|\bat\b\s+"),
        "Modifying scheduled jobs is not allowed",
    ),
    # 12. SSH/credential access
    (
        "ssh_keys",
        re.compile(r"~/.ssh/|/etc/ssh/|id_rsa|id_ed25519|authorized_keys"),
        "Accessing SSH credentials is not allowed",
    ),
    # 13. Password/shadow file access
    (
        "password_files",
        re.compile(r"/etc/passwd|/etc/shadow|/etc/sudoers"),
        "Accessing system credential files is not allowed",
    ),
    # 14. Kernel module operations
    (
        "kernel_modules",
        re.compile(r"\binsmod\b|\brmmod\b|\bmodprobe\b"),
        "Kernel module operations are not allowed",
    ),
    # 15. System control
    (
        "system_control",
        re.compile(r"\breboot\b|\bshutdown\b|\bhalt\b|\bpoweroff\b|\binit\s+[06]"),
        "System control operations are not allowed",
    ),
    # 16. Privilege escalation
    (
        "privilege_escalation",
        re.compile(r"\bsudo\b|\bsu\b\s+-|\bchmod\s+[0-7]*s|\bsetuid\b"),
        "Privilege escalation is not allowed",
    ),
    # 17. IFS injection
    (
        "ifs_injection",
        re.compile(r"\bIFS="),
        "IFS manipulation is not allowed",
    ),
    # 18. Control characters
    (
        "control_chars",
        re.compile(r"[\x00-\x08\x0e-\x1f\x7f]"),
        "Control characters in commands are not allowed",
    ),
    # 19. Hex/octal obfuscation
    (
        "hex_obfuscation",
        re.compile(r"\\x[0-9a-fA-F]{2}|\\[0-7]{3}|\$'\\"),
        "Hex/octal obfuscated sequences are not allowed",
    ),
    # 20. Base64 decode piping
    (
        "base64_decode",
        re.compile(r"base64\s+(-d|--decode).*\|"),
        "Base64 decode piping is not allowed",
    ),
    # 21. /dev/ access (excluding standard devices)
    (
        "dev_access",
        re.compile(r"/dev/(?!null|zero|urandom|random|stdin|stdout|stderr|fd/|tty)"),
        "Accessing /dev/ devices is not allowed",
    ),
    # 22. Network configuration
    (
        "network_config",
        re.compile(r"\biptables\b|\bip6tables\b|\bnft\b|\bufw\b|\bfirewall-cmd\b"),
        "Modifying firewall/network configuration is not allowed",
    ),
    # 23. Container escape patterns
    (
        "container_escape",
        re.compile(r"nsenter|unshare.*--mount|/var/run/docker\.sock"),
        "Container escape patterns are not allowed",
    ),
]


class BashSecurityResult:
    """Result of a bash security check."""

    __slots__ = ("safe", "category", "message")

    def __init__(self, *, safe: bool, category: str = "", message: str = "") -> None:
        self.safe = safe
        self.category = category
        self.message = message


class BashCommandValidator:
    """23-category bash command security validator.

    CC: bashSecurity.ts:77-101. Validates bash commands against known
    dangerous patterns before execution. Configurable via deny/allow
    category overrides.

    V1: Python implementation. Rust acceleration follow-up.
    """

    def __init__(
        self,
        *,
        disabled_categories: set[str] | None = None,
        extra_patterns: list[tuple[str, re.Pattern[str], str]] | None = None,
    ) -> None:
        self._disabled = disabled_categories or set()
        self._checks = list(_BASH_SECURITY_CHECKS)
        if extra_patterns:
            self._checks.extend(extra_patterns)

    def validate(self, command: str) -> BashSecurityResult:
        """Validate a bash command against all security categories.

        Returns BashSecurityResult with safe=False if any check fails.
        """
        for category, pattern, message in self._checks:
            if category in self._disabled:
                continue
            if pattern.search(command):
                return BashSecurityResult(safe=False, category=category, message=message)
        return BashSecurityResult(safe=True)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BashCommandValidator":
        """Load from agent config.

        Config path: settings.agent.bash_security.disabled_categories
        """
        disabled = set(
            config.get("settings", {})
            .get("agent", {})
            .get("bash_security", {})
            .get("disabled_categories", [])
        )
        return cls(disabled_categories=disabled)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_primary_arg(_tool_name: str, args: dict[str, Any]) -> str:
    """Extract the primary string argument for pattern matching.

    Convention: "command" for Bash, "path"/"file_path" for File* tools.
    """
    # Tool-specific primary arg
    for key in ("command", "path", "file_path", "pattern", "directory"):
        if key in args:
            val = args[key]
            return str(val) if val is not None else ""
    # Fallback: first string arg
    for val in args.values():
        if isinstance(val, str):
            return val
    return ""
