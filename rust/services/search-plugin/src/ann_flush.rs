//! Indexing back-pressure primitives (#4777).
//!
//! Two problems made a burst of `IndexDocuments` batches stall the whole
//! node:
//!
//! 1. `do_index_documents` held the per-zone write mutex across every
//!    remote embedding call, so N concurrent batches serialised on the
//!    mutex and each waited for all the others' network round-trips.
//!    [`EmbedGate`] replaces that accidental serialisation with an explicit
//!    concurrency cap that is held ONLY around `embed_batch`, outside the
//!    zone lock.
//! 2. Every batch ended with a full `Hnsw::file_dump` — at a few hundred
//!    thousand chunks that is over a gigabyte of disk writes per batch,
//!    competing with the kernel's own fsyncs on the same volume.
//!    [`AnnFlushCoordinator`] lets a batch skip the dump while other
//!    batches are in flight for the same zone; the last batch of the burst
//!    dumps once, and a fallback flusher thread lands the dump after
//!    [`DEFAULT_ANN_FLUSH_SECONDS`] if the stream never pauses.
//!
//! Crash safety of the deferral: vectors added since the last dump live
//! only in memory, so the documents they belong to are recorded in the
//! zone's `IndexState` with `mtime = None` (the "retry me" verdict) until
//! the dump lands.  The pairs `(path, real_mtime)` are parked in the
//! coordinator and upgraded to the real mtime right after the dump.  A
//! crash in between therefore costs a re-embed of those documents on the
//! next refresh — never a permanent hole — and the zone's `.write-dirty`
//! sentinel stays set for the same window so the query cache is bypassed.
//!
//! The coordinator also tracks the zone's *epoch* — the window in which at
//! least one batch is in flight.  The dirty sentinel is cleared only by the
//! batch that ends the epoch (or by the flusher once the epoch is over),
//! and only when every batch of the epoch converged and the zone was not
//! already dirty from a failed write when the epoch began.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::{Condvar, Mutex};

use crate::ann_index::AnnIndex;
use crate::index_manager::IndexManager;
use crate::index_state::IndexState;
use crate::query_cache::SharedQueryCache;

/// Max concurrent `embed_batch` calls across all IndexDocuments batches.
/// `0` = unlimited.
pub const EMBED_CONCURRENCY_ENV: &str = "NEXUS_SEARCH_EMBED_CONCURRENCY";
pub const DEFAULT_EMBED_CONCURRENCY: usize = 4;

/// Fallback delay before a deferred HNSW dump is forced to disk.  `0`
/// disables deferral entirely (every batch dumps inline, pre-#4777).
pub const ANN_FLUSH_SECONDS_ENV: &str = "NEXUS_SEARCH_ANN_FLUSH_SECONDS";
pub const DEFAULT_ANN_FLUSH_SECONDS: u64 = 30;

/// Zones with at least this many live ANN chunks defer their dump even
/// when no sibling batch is in flight, so a large index is rewritten at
/// most once per [`ANN_FLUSH_SECONDS_ENV`] instead of once per call.
/// Below it, a lone batch dumps inline (immediate durability, no flusher
/// thread).  `0` = always defer.
pub const ANN_DEFER_MIN_CHUNKS_ENV: &str = "NEXUS_SEARCH_ANN_DEFER_MIN_CHUNKS";
pub const DEFAULT_ANN_DEFER_MIN_CHUNKS: usize = 10_000;

/// After the fallback delay, the flusher waits until the zone has seen no
/// index batch for this long before dumping.  The dump holds the zone
/// write lock for as long as it takes to rewrite the whole graph (seconds
/// at 100k+ chunks), so landing it in a pause keeps it off the request
/// path: a dump every [`ANN_FLUSH_SECONDS_ENV`] under steady traffic
/// stalled roughly one index call per window for 2-11 s in production.
/// `0` = no idle gate (dump as soon as the delay elapses).
pub const ANN_FLUSH_IDLE_SECONDS_ENV: &str = "NEXUS_SEARCH_ANN_FLUSH_IDLE_SECONDS";
pub const DEFAULT_ANN_FLUSH_IDLE_SECONDS: u64 = 5;

/// Upper bound on how long the idle gate may postpone a dump from the
/// first deferral of its window — caps the re-embed window after a crash
/// when traffic never pauses.
pub const ANN_FLUSH_MAX_SECONDS_ENV: &str = "NEXUS_SEARCH_ANN_FLUSH_MAX_SECONDS";
pub const DEFAULT_ANN_FLUSH_MAX_SECONDS: u64 = 300;

fn env_usize(name: &str, default: usize) -> usize {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => raw.trim().parse().unwrap_or_else(|_| {
            tracing::warn!(var = name, value = %raw, "not an integer; using default {default}");
            default
        }),
        _ => default,
    }
}

// ── EmbedGate ─────────────────────────────────────────────────────

/// Counting semaphore for blocking-pool threads (tokio's `Semaphore` is
/// async-only and the embed call sites run inside `spawn_blocking`).
pub struct EmbedGate {
    capacity: usize,
    available: Mutex<usize>,
    released: Condvar,
}

impl EmbedGate {
    /// `capacity == 0` means unlimited — `acquire` never blocks.
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            available: Mutex::new(capacity),
            released: Condvar::new(),
        }
    }

    pub fn from_env() -> Self {
        Self::new(env_usize(EMBED_CONCURRENCY_ENV, DEFAULT_EMBED_CONCURRENCY))
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Permits currently free (tests / diagnostics).  Meaningless when
    /// unlimited.
    pub fn available(&self) -> usize {
        *self.available.lock()
    }

    /// Block until a permit is free.
    pub fn acquire(&self) -> EmbedPermit<'_> {
        if self.capacity == 0 {
            return EmbedPermit(None);
        }
        let mut free = self.available.lock();
        while *free == 0 {
            self.released.wait(&mut free);
        }
        *free -= 1;
        EmbedPermit(Some(self))
    }

    /// Non-blocking variant — `None` when every permit is taken.
    pub fn try_acquire(&self) -> Option<EmbedPermit<'_>> {
        if self.capacity == 0 {
            return Some(EmbedPermit(None));
        }
        let mut free = self.available.lock();
        if *free == 0 {
            return None;
        }
        *free -= 1;
        Some(EmbedPermit(Some(self)))
    }
}

/// RAII permit from [`EmbedGate::acquire`].
pub struct EmbedPermit<'a>(Option<&'a EmbedGate>);

impl Drop for EmbedPermit<'_> {
    fn drop(&mut self) {
        if let Some(gate) = self.0 {
            let mut free = gate.available.lock();
            *free += 1;
            gate.released.notify_one();
        }
    }
}

// ── AnnFlushCoordinator ───────────────────────────────────────────

/// A zone's ANN mutations that are in memory but not yet dumped.
pub struct PendingAnnDump {
    /// The live index whose `commit()` is owed.
    pub ann: Arc<AnnIndex>,
    /// `(path, real_mtime)` for documents whose vectors are in memory
    /// only.  Their `IndexState` entries hold `None` until the dump lands.
    pub records: Vec<(String, Option<i64>)>,
    /// Verdict of the epoch that produced this dump, filled in when that
    /// epoch ended: may the zone's dirty sentinel be cleared once the
    /// dump lands?  Stays `true` while the epoch is still running (the
    /// running epoch's `clean` flag carries the verdict instead).
    pub clearable: bool,
    /// First deferral in this pending window.
    pub deferred_at: Instant,
    /// Latest deferral — the flusher waits for the zone to go quiet.
    pub last_deferred_at: Instant,
    flusher_scheduled: bool,
}

#[derive(Default)]
struct ZoneState {
    /// Batches between `begin_batch` and `end_batch`.
    active: usize,
    /// Batches currently blocked on the zone write lock.
    waiting: usize,
    /// The zone carried dirt from a FAILED write (not from our own
    /// pending dump) when the current epoch began — never ours to clear.
    dirty_before: bool,
    /// Every batch of the current epoch converged and saved its state.
    clean: bool,
    pending: Option<PendingAnnDump>,
    /// A batch added FTS documents but left the tantivy commit to a
    /// later batch (siblings were queued on the zone lock).  Whoever
    /// next holds the lock — a sibling's Phase 1, any Phase 3, or the
    /// flusher — must commit before recording state.
    fts_uncommitted: bool,
}

impl ZoneState {
    fn is_idle(&self) -> bool {
        self.active == 0 && self.waiting == 0 && self.pending.is_none() && !self.fts_uncommitted
    }
}

/// Outcome of [`AnnFlushCoordinator::end_batch`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EpochEnd {
    /// This batch was the last one in flight for the zone.
    pub last: bool,
    /// `last` AND every batch converged AND the zone was clean when the
    /// epoch began — the caller may clear the dirty sentinel (subject to
    /// any still-pending dump's own verdict).
    pub clearable: bool,
}

/// Ticket held while a batch waits for its zone's write lock — see
/// [`AnnFlushCoordinator::enter_wait`].
pub struct WaitTicket<'a> {
    coordinator: &'a AnnFlushCoordinator,
    zone_id: String,
}

impl Drop for WaitTicket<'_> {
    fn drop(&mut self) {
        let mut zones = self.coordinator.zones.lock();
        if let Some(z) = zones.get_mut(&self.zone_id) {
            z.waiting = z.waiting.saturating_sub(1);
            if z.is_idle() {
                zones.remove(&self.zone_id);
            }
        }
    }
}

pub struct AnnFlushCoordinator {
    zones: Mutex<HashMap<String, ZoneState>>,
    /// `None` ⇒ deferral disabled; every batch dumps inline.
    flush_delay: Option<Duration>,
    /// Live-chunk count from which a zone's dump is deferred even when no
    /// sibling batch is in flight — see [`ANN_DEFER_MIN_CHUNKS_ENV`].
    defer_min_chunks: usize,
    /// See [`ANN_FLUSH_IDLE_SECONDS_ENV`]; zero disables the gate.
    flush_idle: Duration,
    /// See [`ANN_FLUSH_MAX_SECONDS_ENV`].
    flush_max: Duration,
}

impl AnnFlushCoordinator {
    pub fn new(flush_delay: Option<Duration>) -> Self {
        Self {
            zones: Mutex::new(HashMap::new()),
            flush_delay,
            defer_min_chunks: DEFAULT_ANN_DEFER_MIN_CHUNKS,
            flush_idle: Duration::ZERO,
            flush_max: Duration::from_secs(DEFAULT_ANN_FLUSH_MAX_SECONDS),
        }
    }

    pub fn from_env() -> Self {
        let secs = env_usize(ANN_FLUSH_SECONDS_ENV, DEFAULT_ANN_FLUSH_SECONDS as usize);
        let idle = env_usize(
            ANN_FLUSH_IDLE_SECONDS_ENV,
            DEFAULT_ANN_FLUSH_IDLE_SECONDS as usize,
        );
        let max = env_usize(
            ANN_FLUSH_MAX_SECONDS_ENV,
            DEFAULT_ANN_FLUSH_MAX_SECONDS as usize,
        );
        Self::new((secs > 0).then(|| Duration::from_secs(secs as u64)))
            .with_defer_min_chunks(env_usize(
                ANN_DEFER_MIN_CHUNKS_ENV,
                DEFAULT_ANN_DEFER_MIN_CHUNKS,
            ))
            .with_flush_gate(
                Duration::from_secs(idle as u64),
                Duration::from_secs(max as u64),
            )
    }

    /// Idle gate for the fallback flusher (see [`ANN_FLUSH_IDLE_SECONDS_ENV`]).
    pub fn with_flush_gate(mut self, idle: Duration, max: Duration) -> Self {
        self.flush_idle = idle;
        self.flush_max = max;
        self
    }

    /// How much longer the fallback flusher should wait before dumping
    /// `zone_id`, or `None` to dump now: nothing is pending, the gate is
    /// off, the zone has been quiet (no batch in flight or queued, none
    /// deferred) for the idle period, or the window hit its upper bound.
    pub fn flush_wait(&self, zone_id: &str, now: Instant) -> Option<Duration> {
        if self.flush_idle.is_zero() {
            return None;
        }
        let zones = self.zones.lock();
        let z = zones.get(zone_id)?;
        let pending = z.pending.as_ref()?;
        let age = now.saturating_duration_since(pending.deferred_at);
        if age >= self.flush_max {
            return None;
        }
        let busy = z.active > 0 || z.waiting > 0;
        let quiet = now.saturating_duration_since(pending.last_deferred_at);
        if !busy && quiet >= self.flush_idle {
            return None;
        }
        let wait = if busy {
            self.flush_idle
        } else {
            self.flush_idle - quiet
        };
        Some(
            wait.min(self.flush_max - age)
                .max(Duration::from_millis(50)),
        )
    }

    /// Override the large-index threshold (tests; `0` = every batch defers).
    pub fn with_defer_min_chunks(mut self, n: usize) -> Self {
        self.defer_min_chunks = n;
        self
    }

    pub fn deferral_enabled(&self) -> bool {
        self.flush_delay.is_some()
    }

    pub fn flush_delay(&self) -> Option<Duration> {
        self.flush_delay
    }

    pub fn defer_min_chunks(&self) -> usize {
        self.defer_min_chunks
    }

    /// Should this batch leave its hnsw dump to the flusher?  Yes when
    /// deferral is on AND either a sibling batch is in flight (it will
    /// dump for both) or the index is large enough that an inline dump
    /// would stall the node's disk for seconds (#4777: 1.5 GB at 210 k
    /// chunks, every single-document call, on the volume the kernel
    /// fsyncs to).  Small indexes keep dumping inline so a quiet
    /// deployment stays durable immediately and never wakes a flusher.
    pub fn should_defer(&self, zone_id: &str, live_chunks: usize) -> bool {
        self.deferral_enabled()
            && (self.others_in_flight(zone_id) || live_chunks >= self.defer_min_chunks)
    }

    // ── epoch tracking ──

    /// A batch starts work on `zone_id`.  Call BEFORE the first sink
    /// mutation (and before `mark_zone_dirty`) so the epoch can record
    /// whether the zone was already dirty from someone else's failure.
    pub fn begin_batch(&self, zone_id: &str, manager: &IndexManager) {
        let mut zones = self.zones.lock();
        let z = zones.entry(zone_id.to_string()).or_default();
        if z.active == 0 {
            z.dirty_before = z.pending.is_none() && manager.zone_is_dirty(zone_id);
            z.clean = true;
        }
        z.active += 1;
    }

    /// A batch finished `zone_id` (success or failure).  `batch_clean`
    /// is false when any of its documents did not converge or its state
    /// save failed.  Caller holds the zone write lock on the success
    /// path so the returned verdict cannot be raced by a sibling.
    pub fn end_batch(&self, zone_id: &str, batch_clean: bool) -> EpochEnd {
        let mut zones = self.zones.lock();
        let Some(z) = zones.get_mut(zone_id) else {
            return EpochEnd {
                last: true,
                clearable: false,
            };
        };
        z.clean &= batch_clean;
        z.active = z.active.saturating_sub(1);
        if z.active > 0 {
            return EpochEnd {
                last: false,
                clearable: false,
            };
        }
        let clearable = z.clean && !z.dirty_before;
        if let Some(p) = z.pending.as_mut() {
            p.clearable = clearable;
        }
        if z.is_idle() {
            zones.remove(zone_id);
        }
        EpochEnd {
            last: true,
            clearable,
        }
    }

    /// Batches currently in flight for `zone_id` (including the caller).
    pub fn active(&self, zone_id: &str) -> usize {
        self.zones.lock().get(zone_id).map_or(0, |z| z.active)
    }

    // ── lock queue ──

    /// Register a batch about to block on `zone_id`'s write lock.  Drop
    /// the ticket right after the lock is acquired so `waiters` counts
    /// only batches still queued behind the current holder.
    pub fn enter_wait(&self, zone_id: &str) -> WaitTicket<'_> {
        self.zones
            .lock()
            .entry(zone_id.to_string())
            .or_default()
            .waiting += 1;
        WaitTicket {
            coordinator: self,
            zone_id: zone_id.to_string(),
        }
    }

    /// Batches currently queued on `zone_id`'s write lock.
    pub fn waiters(&self, zone_id: &str) -> usize {
        self.zones.lock().get(zone_id).map_or(0, |z| z.waiting)
    }

    // ── FTS commit coalescing ──

    /// Phase 1 skipped `fts.commit()` because siblings are queued on the
    /// zone lock; the documents sit in tantivy's writer until the next
    /// lock holder commits.  Caller holds the zone write lock.
    pub fn note_fts_skipped(&self, zone_id: &str) {
        self.zones
            .lock()
            .entry(zone_id.to_string())
            .or_default()
            .fts_uncommitted = true;
    }

    /// Some lock holder committed the FTS writer, covering every skipped
    /// batch before it.  Caller holds the zone write lock.
    pub fn note_fts_committed(&self, zone_id: &str) {
        let mut zones = self.zones.lock();
        if let Some(z) = zones.get_mut(zone_id) {
            z.fts_uncommitted = false;
            if z.is_idle() {
                zones.remove(zone_id);
            }
        }
    }

    /// Does the zone's FTS writer hold documents no commit has covered?
    pub fn fts_uncommitted(&self, zone_id: &str) -> bool {
        self.zones
            .lock()
            .get(zone_id)
            .is_some_and(|z| z.fts_uncommitted)
    }

    /// Should the lock holder leave the hnsw dump to a later batch?
    /// True when another batch is queued on the lock or still embedding
    /// (in flight but not yet waiting) — it will reach the lock soon and
    /// can dump for both.
    pub fn others_in_flight(&self, zone_id: &str) -> bool {
        self.zones
            .lock()
            .get(zone_id)
            .is_some_and(|z| z.waiting > 0 || z.active > 1)
    }

    // ── pending dump ──

    pub fn has_pending(&self, zone_id: &str) -> bool {
        self.zones
            .lock()
            .get(zone_id)
            .is_some_and(|z| z.pending.is_some())
    }

    /// Zones with a dump owed (Stats / diagnostics).
    pub fn pending_zone_count(&self) -> usize {
        self.zones
            .lock()
            .values()
            .filter(|z| z.pending.is_some())
            .count()
    }

    /// Hand the owed dump to the caller, who MUST hold the zone write
    /// lock and either commit it or hand it back via [`Self::restore`].
    pub fn take_pending(&self, zone_id: &str) -> Option<PendingAnnDump> {
        let mut zones = self.zones.lock();
        let z = zones.get_mut(zone_id)?;
        let pending = z.pending.take();
        if z.is_idle() {
            zones.remove(zone_id);
        }
        pending
    }

    /// Put a dump back after a failed commit so the next batch or
    /// flusher retries it.
    pub fn restore(&self, zone_id: &str, mut pending: PendingAnnDump) {
        pending.flusher_scheduled = false;
        let mut zones = self.zones.lock();
        let z = zones.entry(zone_id.to_string()).or_default();
        match z.pending.take() {
            Some(mut newer) => {
                // A batch deferred in between (cannot happen under the
                // zone lock, but merge defensively).
                newer.records.extend(pending.records);
                newer.clearable &= pending.clearable;
                newer.deferred_at = newer.deferred_at.min(pending.deferred_at);
                newer.last_deferred_at = newer.last_deferred_at.max(pending.last_deferred_at);
                z.pending = Some(newer);
            }
            None => z.pending = Some(pending),
        }
    }

    /// A batch that is about to (re)write `paths` owns their state from
    /// now on: drop any parked upgrade so a later flush cannot clobber
    /// this batch's verdict.  Caller holds the zone write lock.
    pub fn forget_paths<'p>(&self, zone_id: &str, paths: impl IntoIterator<Item = &'p str>) {
        let mut zones = self.zones.lock();
        let Some(pending) = zones.get_mut(zone_id).and_then(|z| z.pending.as_mut()) else {
            return;
        };
        let owned: HashSet<&str> = paths.into_iter().collect();
        if owned.is_empty() {
            return;
        }
        pending.records.retain(|(p, _)| !owned.contains(p.as_str()));
    }

    /// Park `zone_id`'s dump.  Caller holds the zone write lock and has
    /// already recorded `records`' paths with `mtime = None` in the saved
    /// state.  Schedules the fallback flusher on the first deferral of a
    /// pending window.
    pub fn defer(
        self: &Arc<Self>,
        zone_id: &str,
        ann: Arc<AnnIndex>,
        records: Vec<(String, Option<i64>)>,
        manager: Arc<IndexManager>,
        cache: SharedQueryCache,
    ) {
        let Some(delay) = self.flush_delay else {
            // Deferral disabled — callers check `deferral_enabled` first;
            // reaching here is a programming error, so dump inline.
            if let Err(e) = ann.commit() {
                tracing::warn!(zone = %zone_id, err = %e, "inline ann commit failed");
            }
            return;
        };
        let schedule = {
            let mut zones = self.zones.lock();
            let z = zones.entry(zone_id.to_string()).or_default();
            let entry = z.pending.get_or_insert_with(|| PendingAnnDump {
                ann: Arc::clone(&ann),
                records: Vec::new(),
                clearable: true,
                deferred_at: Instant::now(),
                last_deferred_at: Instant::now(),
                flusher_scheduled: false,
            });
            entry.ann = ann;
            entry.last_deferred_at = Instant::now();
            entry.records.extend(records);
            let schedule = !entry.flusher_scheduled;
            entry.flusher_scheduled = true;
            schedule
        };
        tracing::debug!(zone = %zone_id, "hnsw dump deferred — other index batches in flight");
        if schedule {
            self.spawn_flusher(zone_id.to_string(), manager, cache, delay);
        }
    }

    fn spawn_flusher(
        self: &Arc<Self>,
        zone_id: String,
        manager: Arc<IndexManager>,
        cache: SharedQueryCache,
        delay: Duration,
    ) {
        let coordinator = Arc::clone(self);
        let zone_for_thread = zone_id.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("nexus-ann-flush-{zone_id}"))
            .spawn(move || {
                std::thread::sleep(delay);
                while let Some(wait) = coordinator.flush_wait(&zone_for_thread, Instant::now()) {
                    std::thread::sleep(wait);
                }
                match coordinator.flush_zone(&zone_for_thread, &manager, &cache) {
                    Ok(true) => {
                        tracing::info!(zone = %zone_for_thread, "deferred hnsw dump landed (fallback flusher)")
                    }
                    Ok(false) => {
                        tracing::debug!(zone = %zone_for_thread, "deferred hnsw dump already committed inline")
                    }
                    Err(e) => {
                        tracing::warn!(zone = %zone_for_thread, err = %e, "deferred hnsw dump failed — will retry on next index batch")
                    }
                }
            });
        if let Err(e) = spawned {
            tracing::warn!(zone = %zone_id, err = %e, "could not spawn ann flusher thread; next batch will dump inline");
            if let Some(entry) = self
                .zones
                .lock()
                .get_mut(&zone_id)
                .and_then(|z| z.pending.as_mut())
            {
                entry.flusher_scheduled = false;
            }
        }
    }

    /// Land `zone_id`'s owed dump now: takes the zone write lock, commits
    /// the ANN index, upgrades the parked `(path, mtime)` records, saves
    /// the state and clears the dirty sentinel when permitted.
    ///
    /// Returns `Ok(false)` when nothing was pending (a batch committed it
    /// inline first).  On a commit failure the dump is handed back for a
    /// later retry and the error returned.
    pub fn flush_zone(
        &self,
        zone_id: &str,
        manager: &IndexManager,
        cache: &SharedQueryCache,
    ) -> Result<bool, String> {
        let zone_lock = manager.zone_write_lock(zone_id);
        let _guard = zone_lock.lock();
        // A coalesced FTS commit nobody got to (the burst ended with a
        // failed batch): land it here so those documents become
        // searchable and durable.
        if self.fts_uncommitted(zone_id) {
            match manager.get_or_open(zone_id) {
                Ok(fts) => match fts.commit() {
                    Ok(()) => {
                        self.note_fts_committed(zone_id);
                        cache.invalidate_zone(zone_id);
                    }
                    Err(e) => {
                        tracing::warn!(zone = %zone_id, err = %e, "coalesced fts commit failed in flusher")
                    }
                },
                Err(e) => {
                    tracing::warn!(zone = %zone_id, err = %e, "open fts for coalesced commit failed")
                }
            }
        }
        let Some(pending) = self.take_pending(zone_id) else {
            return Ok(false);
        };
        if let Err(e) = pending.ann.commit() {
            let msg = format!("ann commit for zone {zone_id:?}: {e}");
            self.restore(zone_id, pending);
            return Err(msg);
        }
        let saved = match IndexState::open_or_create(manager.zone_root(zone_id)) {
            Ok(state) => {
                upgrade_records(&state, &pending.records);
                match state.save() {
                    Ok(()) => true,
                    Err(e) => {
                        tracing::warn!(zone = %zone_id, err = %e, "index_state save failed after deferred dump — zone stays cache-bypassed");
                        false
                    }
                }
            }
            Err(e) => {
                tracing::warn!(zone = %zone_id, err = %e, "index_state open failed after deferred dump — zone stays cache-bypassed");
                false
            }
        };
        cache.invalidate_zone(zone_id);
        // Dirty-sentinel verdict: clear now if no epoch is running;
        // otherwise fold this dump's verdict into the running epoch and
        // let its last batch decide.
        let clear_now = {
            let mut zones = self.zones.lock();
            match zones.get_mut(zone_id) {
                Some(z) if z.active > 0 => {
                    z.clean &= saved && pending.clearable;
                    false
                }
                _ => saved && pending.clearable,
            }
        };
        if clear_now {
            manager.clear_zone_dirty(zone_id);
        }
        Ok(true)
    }
}

/// Promote parked `(path, mtime)` pairs whose state entry still says
/// "retry me" (`Some(None)`).  Entries a later batch re-recorded, or
/// forgot, are left alone — that batch owns the verdict.
pub fn upgrade_records(state: &IndexState, records: &[(String, Option<i64>)]) {
    for (path, mtime) in records {
        if state.cached_mtime(path) == Some(None) {
            state.record(path, *mtime);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn park_pending(c: &AnnFlushCoordinator, zone: &str, deferred_at: Instant, last: Instant) {
        let dir = std::env::temp_dir().join(format!(
            "ann-flush-gate-{}-{}",
            std::process::id(),
            deferred_at.elapsed().as_nanos()
        ));
        let ann = AnnIndex::open_or_create(dir, 4).expect("ann");
        c.zones.lock().entry(zone.to_string()).or_default().pending = Some(PendingAnnDump {
            ann,
            records: Vec::new(),
            clearable: true,
            deferred_at,
            last_deferred_at: last,
            flusher_scheduled: true,
        });
    }

    #[test]
    fn flush_waits_for_the_zone_to_go_quiet_up_to_the_cap() {
        let idle = Duration::from_secs(5);
        let max = Duration::from_secs(300);
        let c = AnnFlushCoordinator::new(Some(Duration::from_secs(30))).with_flush_gate(idle, max);
        let t0 = Instant::now();

        // Nothing pending → dump (a no-op) right away.
        assert_eq!(c.flush_wait("z", t0), None);

        // Deferred 1 s ago, no batch in flight → wait out the rest of the idle period.
        park_pending(&c, "z", t0, t0);
        assert_eq!(
            c.flush_wait("z", t0 + Duration::from_secs(1)),
            Some(Duration::from_secs(4))
        );
        // Quiet for the idle period → dump now.
        assert_eq!(c.flush_wait("z", t0 + Duration::from_secs(5)), None);

        // A batch in flight or queued on the zone lock keeps it waiting…
        c.zones.lock().get_mut("z").unwrap().active = 1;
        assert_eq!(c.flush_wait("z", t0 + Duration::from_secs(60)), Some(idle));
        c.zones.lock().get_mut("z").unwrap().active = 0;
        c.zones.lock().get_mut("z").unwrap().waiting = 1;
        assert_eq!(c.flush_wait("z", t0 + Duration::from_secs(60)), Some(idle));
        // …but never past the cap on the window's age.
        assert_eq!(
            c.flush_wait("z", t0 + max - Duration::from_secs(2)),
            Some(Duration::from_secs(2))
        );
        assert_eq!(c.flush_wait("z", t0 + max), None);
    }

    #[test]
    fn flush_gate_zero_dumps_as_soon_as_the_delay_elapses() {
        let c = AnnFlushCoordinator::new(Some(Duration::from_secs(30)));
        let t0 = Instant::now();
        park_pending(&c, "z", t0, t0);
        c.zones.lock().get_mut("z").unwrap().active = 1;
        assert_eq!(c.flush_wait("z", t0), None, "no gate: pre-change behaviour");
    }

    #[test]
    fn embed_gate_bounds_concurrency_and_releases_on_drop() {
        let gate = EmbedGate::new(2);
        let a = gate.acquire();
        let b = gate.acquire();
        assert_eq!(gate.available(), 0);
        assert!(
            gate.try_acquire().is_none(),
            "third permit must not be granted"
        );
        drop(a);
        assert_eq!(gate.available(), 1);
        let c = gate.try_acquire();
        assert!(c.is_some());
        drop(b);
        drop(c);
        assert_eq!(gate.available(), 2);
    }

    #[test]
    fn embed_gate_blocked_acquire_wakes_when_a_permit_frees() {
        let gate = Arc::new(EmbedGate::new(1));
        let held = gate.acquire();
        let gate2 = Arc::clone(&gate);
        let waiter = std::thread::spawn(move || {
            let _permit = gate2.acquire();
            true
        });
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !waiter.is_finished(),
            "waiter must block while the permit is held"
        );
        drop(held);
        assert!(waiter.join().unwrap());
    }

    #[test]
    fn unlimited_gate_never_blocks() {
        let gate = EmbedGate::new(0);
        let _a = gate.acquire();
        let _b = gate.acquire();
        assert!(gate.try_acquire().is_some());
    }

    #[test]
    fn wait_ticket_counts_only_while_held() {
        let coord = AnnFlushCoordinator::new(Some(Duration::from_secs(60)));
        assert_eq!(coord.waiters("z"), 0);
        let t1 = coord.enter_wait("z");
        let t2 = coord.enter_wait("z");
        assert_eq!(coord.waiters("z"), 2);
        assert!(coord.others_in_flight("z"));
        drop(t1);
        assert_eq!(coord.waiters("z"), 1);
        drop(t2);
        assert_eq!(coord.waiters("z"), 0);
        assert!(!coord.others_in_flight("z"));
        assert_eq!(coord.waiters("other"), 0);
    }

    #[test]
    fn epoch_tracks_active_batches_and_clearability() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let manager = IndexManager::with_root(tmp.path().to_path_buf());
        let coord = AnnFlushCoordinator::new(Some(Duration::from_secs(60)));

        coord.begin_batch("z", &manager);
        coord.begin_batch("z", &manager);
        assert_eq!(coord.active("z"), 2);
        assert!(coord.others_in_flight("z"), "a sibling is still embedding");

        // First batch out: not last, no verdict yet.
        assert_eq!(
            coord.end_batch("z", true),
            EpochEnd {
                last: false,
                clearable: false
            }
        );
        assert!(!coord.others_in_flight("z"));
        // Last batch out: epoch verdict.
        assert_eq!(
            coord.end_batch("z", true),
            EpochEnd {
                last: true,
                clearable: true
            }
        );
        assert_eq!(coord.active("z"), 0);

        // A non-converged batch poisons the epoch.
        coord.begin_batch("z", &manager);
        coord.begin_batch("z", &manager);
        coord.end_batch("z", false);
        assert_eq!(
            coord.end_batch("z", true),
            EpochEnd {
                last: true,
                clearable: false
            }
        );
    }

    #[test]
    fn epoch_never_clears_dirt_it_did_not_create() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let manager = IndexManager::with_root(tmp.path().to_path_buf());
        let coord = AnnFlushCoordinator::new(Some(Duration::from_secs(60)));

        // Pre-existing dirt from a failed write.
        manager.mark_zone_dirty("z").expect("mark");
        coord.begin_batch("z", &manager);
        assert_eq!(
            coord.end_batch("z", true),
            EpochEnd {
                last: true,
                clearable: false
            }
        );
    }

    #[test]
    fn fts_uncommitted_flag_round_trips_and_keeps_zone_alive() {
        let coord = AnnFlushCoordinator::new(Some(Duration::from_secs(60)));
        assert!(!coord.fts_uncommitted("z"));
        coord.note_fts_skipped("z");
        assert!(coord.fts_uncommitted("z"));
        // Idle otherwise, but the owed commit keeps the entry.
        assert_eq!(coord.active("z"), 0);
        assert!(coord.fts_uncommitted("z"));
        coord.note_fts_committed("z");
        assert!(!coord.fts_uncommitted("z"));
        // Clearing an unknown zone is a no-op.
        coord.note_fts_committed("nope");
    }

    #[test]
    fn from_env_zero_disables_deferral() {
        assert!(AnnFlushCoordinator::new(None).flush_delay().is_none());
        assert!(!AnnFlushCoordinator::new(None).deferral_enabled());
        assert!(AnnFlushCoordinator::new(Some(Duration::from_secs(5))).deferral_enabled());
    }
}
