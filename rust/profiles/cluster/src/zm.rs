//! Async-safe handle to [`ZoneManager`].
//!
//! `ZoneManager` methods internally `block_on` their own tokio runtime.
//! Calling them from an async context panics ("Cannot start a runtime from
//! within a runtime"). This wrapper offloads each call to
//! [`tokio::task::spawn_blocking`] so the async executor is never blocked.

use std::collections::BTreeMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use nexus_raft::ZoneManager;

#[derive(Clone)]
pub struct Zm(Arc<ZoneManager>);

impl Zm {
    pub fn new(inner: Arc<ZoneManager>) -> Self {
        Self(inner)
    }

    pub fn has_zone(&self, zone_id: &str) -> bool {
        self.0.get_zone(zone_id).is_some()
    }

    pub fn pending_mounts(&self) -> BTreeMap<String, String> {
        self.0.pending_mounts()
    }

    pub async fn create_zone(&self, zone_id: &str, peers: Vec<String>) -> Result<()> {
        let zm = self.0.clone();
        let zid = zone_id.to_string();
        tokio::task::spawn_blocking(move || zm.create_zone(&zid, peers))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        Ok(())
    }

    pub async fn join_zone(
        &self,
        zone_id: &str,
        peers: Vec<String>,
        learner: bool,
    ) -> Result<()> {
        let zm = self.0.clone();
        let zid = zone_id.to_string();
        tokio::task::spawn_blocking(move || zm.join_zone(&zid, peers, learner))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        Ok(())
    }

    pub async fn share_subtree_core(
        &self,
        parent_zone: &str,
        prefix: &str,
        new_zone_id: &str,
    ) -> Result<usize> {
        let zm = self.0.clone();
        let pz = parent_zone.to_string();
        let p = prefix.to_string();
        let nz = new_zone_id.to_string();
        tokio::task::spawn_blocking(move || zm.share_subtree_core(&pz, &p, &nz))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))
    }

    pub async fn mount(
        &self,
        parent_zone: &str,
        path: &str,
        zone_id: &str,
        increment_links: bool,
    ) -> Result<()> {
        let zm = self.0.clone();
        let pz = parent_zone.to_string();
        let p = path.to_string();
        let z = zone_id.to_string();
        tokio::task::spawn_blocking(move || zm.mount(&pz, &p, &z, increment_links))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))
    }

    pub async fn bootstrap_static(
        &self,
        zones: Vec<String>,
        peers: Vec<String>,
        mounts: BTreeMap<String, String>,
    ) -> Result<()> {
        let zm = self.0.clone();
        tokio::task::spawn_blocking(move || zm.bootstrap_static(&zones, peers, &mounts))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))
    }

    pub async fn apply_topology(&self, zone_id: &str) -> Result<bool> {
        let zm = self.0.clone();
        let zid = zone_id.to_string();
        tokio::task::spawn_blocking(move || zm.apply_topology(&zid))
            .await
            .context("task panicked")?
            .map_err(|e| anyhow::anyhow!("{e}"))
    }
}
