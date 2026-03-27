"""Permission primitives (kernel layer).

AllowAllEnforcer deleted in Phase 4 PR 2 — permission enforcement is now
fully delegated to KernelDispatch INTERCEPT hooks (PermissionCheckHook).
No hook registered = no check = zero overhead.

This module is kept for future kernel-level permission primitives
(e.g., PermissionEnforcerProtocol if needed).
"""
