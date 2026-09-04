//! Caller-zone derivation for the protected `/v2/search/*` and
//! `/v2/documents/*` handlers (nexi-lab/nexus#4740).
//!
//! Before this module the handlers took `zone_id` straight from the
//! request body / query string ("empty ⇒ ROOT") and forwarded
//! `root_path` untouched, so an authenticated caller could search or
//! index any zone by naming it, and a caller with no zone at all landed
//! in the root index.  The Python `nexus-server` closed the same gap in
//! `nexus.lib.zone_visibility` + `nexus.lib.zone_scoping`; this is the
//! Rust mirror of those two rules:
//!
//! 1. **The zone comes from the authenticated context**, not the wire.
//!    Admin / system callers may still name a zone explicitly (that is
//!    how an operator inspects a tenant); everyone else gets their own
//!    zone, a mismatching explicit zone is refused, and a credential
//!    with no zone claim is refused rather than routed to ROOT.
//! 2. **Paths are scoped into the zone namespace** (`/zone/<id>/…`) the
//!    way `scope_params_for_zone` does for every RPC syscall, and
//!    results are unscoped back.  A path that names another zone is a
//!    403, never silently re-rooted.  Non-privileged root-zone callers
//!    see the root namespace only.
//!
//! Pure functions — no I/O — so the rules are unit-tested here and the
//! handlers stay thin.

use contracts::operation_context::OperationContext;

/// The root zone id (mirrors `nexus.contracts.constants.ROOT_ZONE_ID`).
pub const ROOT_ZONE_ID: &str = "root";

const ZONE_PREFIX: &str = "/zone/";

/// Why a request could not be attributed to a zone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ZoneError {
    /// Non-admin credential with no zone claim at all.
    NoZoneClaim,
    /// Non-admin credential asked for a zone other than its own.
    Mismatch { requested: String, caller: String },
    /// A path names a zone the caller may not address.
    CrossZonePath { path: String, zone: String },
}

impl std::fmt::Display for ZoneError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ZoneError::NoZoneClaim => write!(
                f,
                "refused: caller has no zone claim; zone-less callers no longer receive the \
                 root view (nexi-lab/nexus#4740)"
            ),
            ZoneError::Mismatch { requested, caller } => write!(
                f,
                "refused: zone_id {requested:?} does not match the caller's zone {caller:?}"
            ),
            ZoneError::CrossZonePath { path, zone } => write!(
                f,
                "refused: path {path:?} is outside the caller's zone {zone:?}"
            ),
        }
    }
}

impl std::error::Error for ZoneError {}

/// Admin and system contexts may address any zone.
pub fn is_privileged(ctx: &OperationContext) -> bool {
    ctx.is_admin || ctx.is_system
}

/// The zone the credential is scoped to: the explicit caller zone when
/// the auth provider set one, else the routing zone.  `None` = no claim.
pub fn caller_zone(ctx: &OperationContext) -> Option<&str> {
    match ctx.context_zone_id.as_deref() {
        Some(z) if !z.is_empty() => Some(z),
        _ if !ctx.zone_id.is_empty() => Some(ctx.zone_id.as_str()),
        _ => None,
    }
}

/// `true` for the root zone in either spelling the wire uses: the explicit
/// id, or the empty string the search proto documents as "server default".
pub fn is_root(zone: &str) -> bool {
    zone.is_empty() || zone == ROOT_ZONE_ID
}

/// Resolve the zone a request operates on.
///
/// * privileged: the request passes through verbatim — an explicit zone
///   is honoured, and an empty one keeps meaning "the backend default
///   (ROOT)" so the admin wire contract is unchanged;
/// * everyone else: the caller's zone — an explicit `requested` must
///   match it, and a missing claim is an error.
pub fn effective_zone(ctx: &OperationContext, requested: &str) -> Result<String, ZoneError> {
    let requested = requested.trim();
    if is_privileged(ctx) {
        return Ok(requested.to_string());
    }
    let caller = caller_zone(ctx).ok_or(ZoneError::NoZoneClaim)?;
    if !requested.is_empty() && requested != caller {
        return Err(ZoneError::Mismatch {
            requested: requested.to_string(),
            caller: caller.to_string(),
        });
    }
    Ok(caller.to_string())
}

/// Zone embedded in an internal `/zone/<id>/…` path.
pub fn embedded_zone(path: &str) -> Option<&str> {
    let rest = path.strip_prefix(ZONE_PREFIX)?;
    let zone = rest.split('/').next().unwrap_or("");
    if zone.is_empty() {
        None
    } else {
        Some(zone)
    }
}

fn collapse_slashes(path: &str) -> String {
    let mut out = path.to_string();
    while out.contains("//") {
        out = out.replace("//", "/");
    }
    out
}

/// Scope a caller-supplied VFS path into `zone`'s namespace.
///
/// Mirrors `nexus.lib.zone_scoping.scope_single_path`: the root zone sees
/// the whole tree unchanged; any other zone gets `/zone/<id>` prefixed,
/// and a path already carrying a *different* zone prefix is refused.
pub fn scope_path(zone: &str, path: &str) -> Result<String, ZoneError> {
    let canonical = collapse_slashes(path);
    if is_root(zone) {
        return Ok(canonical);
    }
    if canonical.starts_with(ZONE_PREFIX) {
        return match embedded_zone(&canonical) {
            Some(embedded) if embedded == zone => Ok(canonical),
            _ => Err(ZoneError::CrossZonePath {
                path: path.to_string(),
                zone: zone.to_string(),
            }),
        };
    }
    let prefix = format!("{ZONE_PREFIX}{zone}");
    if canonical == "/" {
        return Ok(prefix);
    }
    if canonical.starts_with('/') {
        Ok(format!("{prefix}{canonical}"))
    } else {
        Ok(format!("{prefix}/{canonical}"))
    }
}

/// [`scope_path`] plus the root-namespace rule: a non-privileged caller in
/// the root zone may not address another zone's namespace either.
pub fn scope_request_path(
    ctx: &OperationContext,
    zone: &str,
    path: &str,
) -> Result<String, ZoneError> {
    let scoped = scope_path(zone, path)?;
    if !is_privileged(ctx) && is_root(zone) && embedded_zone(&scoped).is_some() {
        return Err(ZoneError::CrossZonePath {
            path: path.to_string(),
            zone: zone.to_string(),
        });
    }
    Ok(scoped)
}

/// Whether an internal result path belongs to `zone`'s view: paths inside
/// a zone namespace belong to that zone; paths outside any namespace are
/// the root zone's.
pub fn visible_in_zone(zone: &str, path: &str) -> bool {
    match embedded_zone(path) {
        Some(embedded) => embedded == zone,
        None => is_root(zone),
    }
}

/// Strip `zone`'s own prefix from an internal path; other paths pass
/// through unchanged (so a privileged cross-zone listing stays
/// zone-qualified, as the Python RPC adapter does for `all_zones`).
pub fn unscope_path(zone: &str, path: &str) -> String {
    if is_root(zone) {
        return path.to_string();
    }
    let prefix = format!("{ZONE_PREFIX}{zone}");
    if path == prefix {
        return "/".to_string();
    }
    match path.strip_prefix(&format!("{prefix}/")) {
        Some(rest) => format!("/{rest}"),
        None => path.to_string(),
    }
}

/// Filter + unscope result paths for the caller: privileged callers keep
/// everything (zone-qualified), everyone else sees only their zone.
pub fn present_paths<I>(ctx: &OperationContext, zone: &str, paths: I) -> Vec<String>
where
    I: IntoIterator<Item = String>,
{
    let privileged = is_privileged(ctx);
    paths
        .into_iter()
        .filter(|p| privileged || visible_in_zone(zone, p))
        .map(|p| unscope_path(zone, &p))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tenant(zone: &str) -> OperationContext {
        let mut ctx = OperationContext::new("alice", ROOT_ZONE_ID, false, None, false);
        ctx.context_zone_id = Some(zone.to_string());
        ctx
    }

    fn admin() -> OperationContext {
        OperationContext::new("root-op", ROOT_ZONE_ID, true, None, false)
    }

    fn zoneless() -> OperationContext {
        let mut ctx = OperationContext::new("nozone", "", false, None, false);
        ctx.context_zone_id = None;
        ctx
    }

    #[test]
    fn caller_zone_prefers_context_zone_then_routing_zone() {
        assert_eq!(caller_zone(&tenant("ta")), Some("ta"));
        let routing_only = OperationContext::new("bob", "tb", false, None, false);
        assert_eq!(caller_zone(&routing_only), Some("tb"));
        assert_eq!(caller_zone(&zoneless()), None);
    }

    #[test]
    fn tenant_gets_its_own_zone_and_cannot_pick_another() {
        assert_eq!(effective_zone(&tenant("ta"), "").unwrap(), "ta");
        assert_eq!(effective_zone(&tenant("ta"), "ta").unwrap(), "ta");
        assert_eq!(
            effective_zone(&tenant("ta"), "tb"),
            Err(ZoneError::Mismatch {
                requested: "tb".into(),
                caller: "ta".into()
            })
        );
    }

    #[test]
    fn zone_less_non_admin_is_refused_not_rooted() {
        assert_eq!(effective_zone(&zoneless(), ""), Err(ZoneError::NoZoneClaim));
        assert_eq!(
            effective_zone(&zoneless(), "ta"),
            Err(ZoneError::NoZoneClaim)
        );
    }

    #[test]
    fn admin_requests_pass_through_verbatim() {
        // empty keeps meaning "backend default" so the admin wire contract
        // (`index_defaults_reach_backend_intact`) is unchanged
        assert_eq!(effective_zone(&admin(), "").unwrap(), "");
        assert_eq!(effective_zone(&admin(), "tb").unwrap(), "tb");
        assert!(is_root("") && is_root(ROOT_ZONE_ID) && !is_root("ta"));
        assert_eq!(scope_path("", "/docs").unwrap(), "/docs");
        assert_eq!(unscope_path("", "/zone/ta/a.txt"), "/zone/ta/a.txt");
    }

    #[test]
    fn scope_path_mirrors_python_scoping() {
        assert_eq!(scope_path("ta", "/").unwrap(), "/zone/ta");
        assert_eq!(
            scope_path("ta", "/docs/a.txt").unwrap(),
            "/zone/ta/docs/a.txt"
        );
        assert_eq!(
            scope_path("ta", "docs//a.txt").unwrap(),
            "/zone/ta/docs/a.txt"
        );
        assert_eq!(
            scope_path("ta", "/zone/ta/a.txt").unwrap(),
            "/zone/ta/a.txt"
        );
        assert!(matches!(
            scope_path("ta", "/zone/tb/a.txt"),
            Err(ZoneError::CrossZonePath { .. })
        ));
        // //zone//tb slips past a naive prefix check; canonicalise first.
        assert!(matches!(
            scope_path("ta", "//zone//tb/x"),
            Err(ZoneError::CrossZonePath { .. })
        ));
        assert_eq!(
            scope_path(ROOT_ZONE_ID, "/zone/tb/a.txt").unwrap(),
            "/zone/tb/a.txt"
        );
    }

    #[test]
    fn root_zone_non_admin_cannot_address_tenant_namespaces() {
        let root_user = OperationContext::new("carol", ROOT_ZONE_ID, false, None, false);
        assert_eq!(
            scope_request_path(&root_user, ROOT_ZONE_ID, "/docs").unwrap(),
            "/docs"
        );
        assert!(matches!(
            scope_request_path(&root_user, ROOT_ZONE_ID, "/zone/tb/x"),
            Err(ZoneError::CrossZonePath { .. })
        ));
        assert_eq!(
            scope_request_path(&admin(), ROOT_ZONE_ID, "/zone/tb/x").unwrap(),
            "/zone/tb/x"
        );
    }

    #[test]
    fn present_paths_filters_and_unscopes_for_tenants_only() {
        let hits = vec![
            "/zone/ta/a.txt".to_string(),
            "/zone/tb/b.txt".to_string(),
            "/root.txt".to_string(),
        ];
        assert_eq!(
            present_paths(&tenant("ta"), "ta", hits.clone()),
            vec!["/a.txt"]
        );
        let root_user = OperationContext::new("carol", ROOT_ZONE_ID, false, None, false);
        assert_eq!(
            present_paths(&root_user, ROOT_ZONE_ID, hits.clone()),
            vec!["/root.txt"]
        );
        // privileged callers keep everything, zone-qualified
        assert_eq!(present_paths(&admin(), ROOT_ZONE_ID, hits.clone()), hits);
        // an admin inspecting one zone gets that zone's view unscoped
        assert_eq!(
            present_paths(&admin(), "ta", hits),
            vec!["/a.txt", "/zone/tb/b.txt", "/root.txt"]
        );
    }

    #[test]
    fn unscope_only_strips_own_prefix() {
        assert_eq!(unscope_path("ta", "/zone/ta"), "/");
        assert_eq!(unscope_path("ta", "/zone/ta/a.txt"), "/a.txt");
        assert_eq!(unscope_path("ta", "/zone/tb/b.txt"), "/zone/tb/b.txt");
        assert_eq!(
            unscope_path(ROOT_ZONE_ID, "/zone/ta/a.txt"),
            "/zone/ta/a.txt"
        );
    }
}
