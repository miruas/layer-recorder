// SPDX-License-Identifier: GPL-3.0-only
//! Pure display hotplug reconciliation.
//!
//! GSR's inventory is authoritative. This module only decides what should
//! happen to independent video tracks; it never spawns, stops or restarts a
//! process. The daemon keeps the hard decision loop, retry accounting and
//! generation stamping. Audio tracks are deliberately excluded.
//!
//! Reconciliation is keyed by the GSR display key (normally `display:<name>`).
//! A change in geometry, mode or command changes the fingerprint and therefore
//! retires the old producer before a new generation starts. A disconnected
//! display retires only its own track. A failed/transient inventory snapshot
//! never produces retirements, so a broken probe cannot tear down a session.

use anyhow::{Context, Result, bail};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

/// Recommended display inventory poll interval, in seconds.
pub const POLL_INTERVAL: f64 = 2.0;
/// First dead-process retry delay, in seconds.
pub const RETRY_BASE: f64 = 2.0;
/// Maximum dead-process retry delay, in seconds.
pub const RETRY_MAX: f64 = 30.0;

const MAX_DISPLAYS: usize = 128;
const MAX_KEY: usize = 512;
const MAX_NAME: usize = 256;
const MAX_TEXT: usize = 8_192;
const MAX_COMMAND_ARGS: usize = 512;

/// Fields that affect the media produced by a display producer. Labels and
/// volatile status fields are intentionally excluded so cosmetic changes do
/// not restart a healthy track.
const FINGERPRINT_FIELDS: &[&str] = &[
    "capture_width", "capture_height",
    "x",
    "y",
    "width",
    "height",
    "logical_width",
    "logical_height",
    "layout_provider",
    "mode",
    "modes",
    "current_mode",
    "rotation",
    "transform",
    "scale",
    "refresh",
    "refresh_rate",
    "fps",
    "selector",
    "capture_name",
    "backend",
    "geometry",
    "device",
    "format",
];

const VOLATILE_FIELDS: &[&str] = &[
    "id",
    "alive",
    "active",
    "closed",
    "generation",
    "hotplug_generation",
    "fingerprint",
    "hotplug_fingerprint",
    "last_attempt",
    "hotplug_last_attempt",
    "failures",
    "hotplug_failures",
    "retry_at",
    "hotplug_retry_at",
    "hotplug_dead",
    "command",
    "type",
];

/// One desired display track, validated from GSR inventory / video-plan JSON.
#[derive(Clone, Debug, PartialEq)]
pub struct DisplaySpec {
    pub key: String,
    pub name: String,
    pub fingerprint: String,
    pub meta: Value,
    pub command: Vec<String>,
}

/// Minimal current-track descriptor supplied by the daemon for reconciliation.
#[derive(Clone, Debug, PartialEq)]
pub struct CurrentTrack {
    pub id: String,
    pub key: String,
    pub fingerprint: String,
    pub generation: u64,
    pub alive: bool,
    pub closed: bool,
    pub last_attempt: Option<f64>,
    pub failures: u32,
    pub retry_at: Option<f64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RetireReason {
    /// The display disappeared from authoritative inventory.
    Disconnected,
    /// Geometry, mode or command changed, or a duplicate generation exists.
    Superseded,
    /// A second live producer was found for the same key/fingerprint.
    Duplicate,
    /// The producer died and its retry is now starting.
    Failed,
}

impl RetireReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Disconnected => "disconnected",
            Self::Superseded => "superseded",
            Self::Duplicate => "duplicate",
            Self::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Retirement {
    pub id: String,
    pub key: String,
    pub fingerprint: String,
    pub generation: u64,
    pub reason: RetireReason,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpawnReason {
    Added,
    Changed,
    Replugged,
    Retry,
}

impl SpawnReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Added => "added",
            Self::Changed => "changed",
            Self::Replugged => "replugged",
            Self::Retry => "retry",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Spawn {
    pub display: DisplaySpec,
    pub generation: u64,
    /// Consecutive failure count to seed on the new track. Non-retry spawns
    /// start at zero; retries carry the previous count so backoff can grow.
    pub failures: u32,
    pub reason: SpawnReason,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Deferred {
    pub key: String,
    pub fingerprint: String,
    pub failures: u32,
    pub retry_at: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlanStatus {
    /// A valid snapshot was reconciled.
    Ready,
    /// The source is an explicit static/portal/window/camera selection.
    Static,
    /// The probe failed or returned invalid data. No actions were emitted.
    ProbeFailed,
}

/// A pure desired-vs-current plan. Additions include retries; retirements must
/// be applied before additions so the same key can move to a new generation.
#[derive(Clone, Debug, PartialEq)]
pub struct ReconcilePlan {
    pub status: PlanStatus,
    pub additions: Vec<Spawn>,
    pub retirements: Vec<Retirement>,
    pub unchanged: Vec<String>,
    pub deferred: Vec<Deferred>,
    pub desired: usize,
    pub current: usize,
    pub error: Option<String>,
}

impl ReconcilePlan {
    fn new(status: PlanStatus) -> Self {
        Self {
            status,
            additions: vec![],
            retirements: vec![],
            unchanged: vec![],
            deferred: vec![],
            desired: 0,
            current: 0,
            error: None,
        }
    }

    fn blocked(status: PlanStatus, error: impl Into<String>) -> Self {
        let mut plan = Self::new(status);
        plan.error = Some(error.into());
        plan
    }

    pub fn has_actions(&self) -> bool {
        !self.additions.is_empty() || !self.retirements.is_empty() || !self.deferred.is_empty()
    }
}

/// A probe result. `Empty` is a successful empty inventory, distinct from a
/// failed probe. `Static` means periodic rediscovery must not run.
#[derive(Clone, Debug, PartialEq)]
pub enum InventorySnapshot {
    Ready(Vec<DisplaySpec>),
    Empty,
    Static,
    Failed { error: String, transient: bool },
}

/// Parse a strict, bounded list of desired display tracks.
///
/// Entries may be video-plan tracks (`key`, `meta`, `command`) or GSR display
/// records (`name`, `width`, ...). Audio entries are rejected: this helper must
/// never reconcile audio tracks.
pub fn parse_display_list(value: &Value) -> Result<Vec<DisplaySpec>> {
    let items = value
        .as_array()
        .context("Display inventory must be a JSON array")?;
    if items.len() > MAX_DISPLAYS {
        bail!("Display inventory has too many entries");
    }
    let mut seen = BTreeSet::new();
    let mut result = vec![];
    for (index, item) in items.iter().enumerate() {
        let display = DisplaySpec::from_value(item)
            .with_context(|| format!("Invalid display inventory entry {index}"))?;
        if !seen.insert(display.key.clone()) {
            bail!("Duplicate display key: {}", display.key);
        }
        result.push(display);
    }
    Ok(result)
}

impl DisplaySpec {
    pub fn from_value(value: &Value) -> Result<Self> {
        let object = value
            .as_object()
            .context("Display entry must be a JSON object")?;
        let mut meta = meta_object(object)?;
        let type_value = object.get("type").or_else(|| meta.get("type"));
        if let Some(kind) = type_value.and_then(Value::as_str) {
            if kind != "video" {
                bail!("Display entry must have type video, not {kind}");
            }
        }
        let explicit_fingerprint = object
            .get("fingerprint")
            .or_else(|| meta.get("fingerprint"))
            .or_else(|| meta.get("hotplug_fingerprint"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let command = parse_command(object.get("command").or_else(|| meta.get("command")))?;
        let key_value = object
            .get("key")
            .or_else(|| meta.get("key"))
            .and_then(text_value);
        let name_value = object
            .get("name")
            .or_else(|| meta.get("name"))
            .and_then(text_value);
        let name = match name_value {
            Some(name) => name,
            None => key_value
                .as_deref()
                .and_then(|key| key.strip_prefix("display:"))
                .context("Display entry needs a name")?
                .to_string(),
        };
        validate_name(&name)?;
        let key = key_value.unwrap_or_else(|| format!("display:{name}"));
        validate_key(&key)?;
        for field in VOLATILE_FIELDS {
            meta.remove(*field);
        }
        meta.insert("type".into(), json!("video"));
        let fingerprint = match explicit_fingerprint {
            Some(fingerprint) => {
                validate_fingerprint(&fingerprint)?;
                fingerprint
            }
            None => fingerprint_for(&name, &meta, &command),
        };
        Ok(Self {
            key,
            name,
            fingerprint,
            meta: Value::Object(meta),
            command,
        })
    }

    /// Metadata to store on the spawned track. The daemon can use this marker
    /// to reconstruct `CurrentTrack` without keeping a parallel process table.
    pub fn stamped_meta(&self, generation: u64) -> Value {
        let mut meta = self.meta.clone();
        if let Some(object) = meta.as_object_mut() {
            object.insert("hotplug_fingerprint".into(), json!(self.fingerprint));
            object.insert("hotplug_generation".into(), json!(generation));
        }
        meta
    }
}

/// Parse current video-track descriptors. Audio descriptors are skipped, not
/// rejected, because the daemon may pass its full track table.
pub fn parse_current_list(value: &Value) -> Result<Vec<CurrentTrack>> {
    let items = value
        .as_array()
        .context("Current tracks must be a JSON array")?;
    let mut result = vec![];
    for (index, item) in items.iter().enumerate() {
        match CurrentTrack::from_value(item)
            .with_context(|| format!("Invalid current track entry {index}"))?
        {
            Some(track) => result.push(track),
            None => (),
        }
    }
    Ok(result)
}

impl CurrentTrack {
    pub fn from_value(value: &Value) -> Result<Option<Self>> {
        let object = value
            .as_object()
            .context("Current track must be a JSON object")?;
        let mut meta = meta_object(object)?;
        let type_value = object.get("type").or_else(|| meta.get("type"));
        if type_value.and_then(Value::as_str) == Some("audio") {
            return Ok(None);
        }
        if let Some(kind) = type_value.and_then(Value::as_str) {
            if kind != "video" {
                bail!("Current display track must have type video, not {kind}");
            }
        }
        let explicit_fingerprint = object
            .get("fingerprint")
            .or_else(|| meta.get("hotplug_fingerprint"))
            .or_else(|| meta.get("fingerprint"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let command = parse_command(object.get("command").or_else(|| meta.get("command")))?;
        let key_value = object
            .get("key")
            .or_else(|| meta.get("key"))
            .and_then(text_value);
        let name_value = object
            .get("name")
            .or_else(|| meta.get("name"))
            .and_then(text_value);
        let name = match name_value {
            Some(name) => name,
            None => key_value
                .as_deref()
                .and_then(|key| key.strip_prefix("display:"))
                .context("Current track needs a name or display key")?
                .to_string(),
        };
        validate_name(&name)?;
        let key = key_value.unwrap_or_else(|| format!("display:{name}"));
        validate_key(&key)?;
        let closed = bool_field(object, &meta, &["closed"]).unwrap_or(false);
        let alive = bool_field(object, &meta, &["alive", "active"]).unwrap_or(!closed);
        let generation = u64_field(object, &meta, &["generation", "hotplug_generation"]).unwrap_or(1);
        let failures = u64_field(object, &meta, &["failures", "hotplug_failures"])
            .unwrap_or(0)
            .min(u32::MAX as u64) as u32;
        let last_attempt = number_field(
            object,
            &meta,
            &["last_attempt", "hotplug_last_attempt"],
        );
        let retry_at = number_field(object, &meta, &["retry_at", "hotplug_retry_at"]);
        for field in VOLATILE_FIELDS {
            meta.remove(*field);
        }
        let fingerprint = match explicit_fingerprint {
            Some(fingerprint) => {
                validate_fingerprint(&fingerprint)?;
                fingerprint
            }
            None => fingerprint_for(&name, &meta, &command),
        };
        let id = object
            .get("id")
            .and_then(text_value)
            .unwrap_or_else(|| format!("{key}:{generation}"));
        validate_key(&id)?;
        Ok(Some(Self {
            id,
            key,
            fingerprint,
            generation,
            alive,
            closed,
            last_attempt,
            failures,
            retry_at,
        }))
    }
}

/// Reconcile a validated desired list against current video tracks.
pub fn reconcile(desired: &[DisplaySpec], current: &[CurrentTrack], now: f64) -> ReconcilePlan {
    let mut plan = ReconcilePlan::new(PlanStatus::Ready);
    plan.desired = desired.len();
    plan.current = current.len();
    let desired_keys: BTreeSet<&str> = desired.iter().map(|item| item.key.as_str()).collect();
    let mut by_key: BTreeMap<&str, Vec<&CurrentTrack>> = BTreeMap::new();
    for track in current {
        by_key.entry(track.key.as_str()).or_default().push(track);
    }

    for (key, tracks) in &by_key {
        if !desired_keys.contains(key) {
            for track in tracks.iter().copied().filter(|track| !track.closed) {
                plan.retirements
                    .push(retirement(track, RetireReason::Disconnected));
            }
        }
    }

    for display in desired {
        let tracks = by_key
            .get(display.key.as_str())
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let live: Vec<&CurrentTrack> = tracks
            .iter()
            .copied()
            .filter(|track| track.alive && !track.closed)
            .collect();
        let matching_live = live
            .iter()
            .copied()
            .filter(|track| track.fingerprint == display.fingerprint)
            .max_by_key(|track| track.generation);
        if let Some(keep) = matching_live {
            plan.unchanged.push(display.key.clone());
            for track in live.iter().copied().filter(|track| track.id != keep.id) {
                let reason = if track.fingerprint == display.fingerprint {
                    RetireReason::Duplicate
                } else {
                    RetireReason::Superseded
                };
                plan.retirements.push(retirement(track, reason));
            }
            continue;
        }

        if !live.is_empty() {
            // Geometry/mode/command changed while an old producer is alive.
            for track in live {
                plan.retirements
                    .push(retirement(track, RetireReason::Superseded));
            }
            plan.additions.push(Spawn {
                display: display.clone(),
                generation: next_generation(tracks),
                failures: 0,
                reason: SpawnReason::Changed,
            });
            continue;
        }

        // A process died without being deliberately closed. Respect backoff so
        // an immediately crashing capture command cannot spin the daemon.
        let dead_matching: Vec<&CurrentTrack> = tracks
            .iter()
            .copied()
            .filter(|track| {
                !track.closed && !track.alive && track.fingerprint == display.fingerprint
            })
            .collect();
        if !dead_matching.is_empty() {
            let state = dead_matching
                .iter()
                .copied()
                .max_by_key(|track| track.generation)
                .expect("dead_matching is not empty");
            let retry_at = retry_at(state);
            if now < retry_at {
                plan.deferred.push(Deferred {
                    key: display.key.clone(),
                    fingerprint: display.fingerprint.clone(),
                    failures: state.failures,
                    retry_at,
                });
                continue;
            }
            plan.additions.push(Spawn {
                display: display.clone(),
                generation: next_generation(tracks),
                failures: state.failures,
                reason: SpawnReason::Retry,
            });
            for track in dead_matching {
                plan.retirements
                    .push(retirement(track, RetireReason::Failed));
            }
            continue;
        }

        // Dead, unclosed tracks with an obsolete fingerprint are superseded.
        for track in tracks
            .iter()
            .copied()
            .filter(|track| !track.closed && !track.alive)
        {
            plan.retirements
                .push(retirement(track, RetireReason::Superseded));
        }
        let reason = if tracks.is_empty() {
            SpawnReason::Added
        } else if tracks
            .iter()
            .any(|track| track.fingerprint == display.fingerprint)
        {
            // The display was deliberately retired and has now returned.
            SpawnReason::Replugged
        } else {
            SpawnReason::Changed
        };
        plan.additions.push(Spawn {
            display: display.clone(),
            generation: next_generation(tracks),
            failures: 0,
            reason,
        });
    }
    plan
}

/// Reconcile a probe snapshot. Only a ready or successfully-empty snapshot may
/// retire tracks. A failed probe and a static source emit no actions.
pub fn reconcile_snapshot(
    snapshot: &InventorySnapshot,
    current: &[CurrentTrack],
    now: f64,
) -> ReconcilePlan {
    match snapshot {
        InventorySnapshot::Ready(desired) => reconcile(desired, current, now),
        InventorySnapshot::Empty => reconcile(&[], current, now),
        InventorySnapshot::Static => {
            let mut plan = ReconcilePlan::new(PlanStatus::Static);
            plan.current = current.len();
            plan
        }
        InventorySnapshot::Failed { error, .. } => {
            ReconcilePlan::blocked(PlanStatus::ProbeFailed, error.clone())
        }
    }
}

/// Convert a Python `video-plan` response into a snapshot without allowing a
/// malformed response to be mistaken for an empty inventory.
pub fn snapshot_from_video_plan(value: &Value) -> InventorySnapshot {
    if value.get("ok").and_then(Value::as_bool) == Some(false) {
        let error = value
            .get("error")
            .and_then(Value::as_str)
            .filter(|error| !error.is_empty())
            .unwrap_or("Display inventory probe failed");
        return InventorySnapshot::Failed {
            error: error.to_string(),
            transient: value
                .get("transient")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        };
    }
    if value.get("poll").and_then(Value::as_bool) == Some(false)
        || value.get("static").and_then(Value::as_bool) == Some(true)
    {
        return InventorySnapshot::Static;
    }
    if value.get("empty").and_then(Value::as_bool) == Some(true) {
        return InventorySnapshot::Empty;
    }
    let list = value
        .get("tracks")
        .or_else(|| value.get("displays"))
        .cloned();
    let Some(list) = list else {
        return InventorySnapshot::Failed {error:"Video-plan response has no display list".into(),transient:false};
    };
    match parse_display_list(&list) {
        Ok(items) if items.is_empty() => InventorySnapshot::Empty,
        Ok(items) => InventorySnapshot::Ready(items),
        Err(error) => InventorySnapshot::Failed {
            error: format!("Invalid video-plan inventory: {error:#}"),
            transient: false,
        },
    }
}

/// Dead-process retry delay, exponential with a hard cap.
pub fn retry_delay(failures: u32) -> f64 {
    let exponent = failures.saturating_sub(1).min(5);
    (RETRY_BASE * 2f64.powi(exponent as i32)).min(RETRY_MAX)
}

/// Absolute time at which the next retry is allowed.
pub fn retry_at(track: &CurrentTrack) -> f64 {
    if let Some(value) = track.retry_at {
        return value;
    }
    track.last_attempt.unwrap_or(0.) + retry_delay(track.failures.max(1))
}

fn retirement(track: &CurrentTrack, reason: RetireReason) -> Retirement {
    Retirement {
        id: track.id.clone(),
        key: track.key.clone(),
        fingerprint: track.fingerprint.clone(),
        generation: track.generation,
        reason,
    }
}

fn next_generation(tracks: &[&CurrentTrack]) -> u64 {
    tracks
        .iter()
        .map(|track| track.generation)
        .max()
        .unwrap_or(0)
        .saturating_add(1)
}

fn meta_object(object: &Map<String, Value>) -> Result<Map<String, Value>> {
    match object.get("meta") {
        Some(Value::Object(meta)) => Ok(meta.clone()),
        Some(_) => bail!("Display meta must be a JSON object"),
        None => Ok(object.clone()),
    }
}

fn parse_command(value: Option<&Value>) -> Result<Vec<String>> {
    let Some(value) = value else {
        return Ok(vec![]);
    };
    let items = value
        .as_array()
        .context("Capture command must be a JSON array")?;
    if items.len() > MAX_COMMAND_ARGS {
        bail!("Capture command has too many arguments");
    }
    let mut command = Vec::with_capacity(items.len());
    for argument in items {
        let text = argument
            .as_str()
            .context("Capture command arguments must be strings")?;
        validate_text(text, "Capture command argument", MAX_TEXT)?;
        command.push(text.to_string());
    }
    Ok(command)
}

fn fingerprint_for(name: &str, meta: &Map<String, Value>, command: &[String]) -> String {
    let mut fields = Map::new();
    for field in FINGERPRINT_FIELDS {
        if let Some(value) = meta.get(*field) {
            fields.insert((*field).to_string(), value.clone());
        }
    }
    let payload = json!({
        "schema": 1,
        "name": name,
        "fields": fields,
        "command": command,
    });
    serde_json::to_string(&payload).unwrap_or_default()
}

fn validate_name(name: &str) -> Result<()> {
    validate_text(name, "Display name", MAX_NAME)?;
    if name.contains('|') {
        bail!("Display name must not contain '|'");
    }
    Ok(())
}

fn validate_key(key: &str) -> Result<()> {
    validate_text(key, "Track key", MAX_KEY)
}

fn validate_fingerprint(fingerprint: &str) -> Result<()> {
    validate_text(fingerprint, "Fingerprint", MAX_TEXT)
}

fn validate_text(value: &str, what: &str, maximum: usize) -> Result<()> {
    if value.trim().is_empty() {
        bail!("{what} cannot be empty");
    }
    if value.len() > maximum {
        bail!("{what} is too long");
    }
    if value.chars().any(char::is_control) {
        bail!("{what} must not contain control characters");
    }
    Ok(())
}

fn text_value(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|text| !text.trim().is_empty())
        .map(str::to_string)
}

fn bool_field(
    object: &Map<String, Value>,
    meta: &Map<String, Value>,
    names: &[&str],
) -> Option<bool> {
    names
        .iter()
        .find_map(|name| object.get(*name).or_else(|| meta.get(*name)))
        .and_then(Value::as_bool)
}

fn u64_field(
    object: &Map<String, Value>,
    meta: &Map<String, Value>,
    names: &[&str],
) -> Option<u64> {
    names
        .iter()
        .find_map(|name| object.get(*name).or_else(|| meta.get(*name)))
        .and_then(Value::as_u64)
}

fn number_field(
    object: &Map<String, Value>,
    meta: &Map<String, Value>,
    names: &[&str],
) -> Option<f64> {
    names
        .iter()
        .find_map(|name| object.get(*name).or_else(|| meta.get(*name)))
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn display(name: &str, width: u64, command: &[&str]) -> DisplaySpec {
        DisplaySpec::from_value(&json!({
            "key": format!("display:{name}"),
            "meta": {"type":"video","name":name,"width":width,"height":1080,"x":0,"logical_width":width,"logical_height":1080},
            "command": command,
        }))
        .unwrap()
    }

    fn current(display: &DisplaySpec, id: &str, generation: u64) -> CurrentTrack {
        CurrentTrack {
            id: id.into(),
            key: display.key.clone(),
            fingerprint: display.fingerprint.clone(),
            generation,
            alive: true,
            closed: false,
            last_attempt: None,
            failures: 0,
            retry_at: None,
        }
    }

    #[test]
    fn unchanged_display_has_no_actions() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let plan = reconcile(
            std::slice::from_ref(&wanted),
            &[current(&wanted, "track-1", 1)],
            10.,
        );
        assert_eq!(plan.status, PlanStatus::Ready);
        assert_eq!(plan.unchanged, vec!["display:DP-1"]);
        assert!(plan.additions.is_empty());
        assert!(plan.retirements.is_empty());
        assert!(plan.deferred.is_empty());
    }

    #[test]
    fn disconnect_retires_only_the_missing_display() {
        let left = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let right = display("DP-2", 1280, &["gsr", "-w", "DP-2"]);
        let plan = reconcile(
            std::slice::from_ref(&left),
            &[current(&left, "left-1", 1), current(&right, "right-1", 1)],
            10.,
        );
        assert_eq!(plan.unchanged, vec!["display:DP-1"]);
        assert_eq!(plan.retirements.len(), 1);
        assert_eq!(plan.retirements[0].id, "right-1");
        assert_eq!(plan.retirements[0].reason, RetireReason::Disconnected);
        assert!(plan.additions.is_empty());
    }

    #[test]
    fn reconnect_after_retirement_starts_a_new_generation() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let mut retired = current(&wanted, "old-1", 4);
        retired.alive = false;
        retired.closed = true;
        let plan = reconcile(std::slice::from_ref(&wanted), &[retired], 10.);
        assert_eq!(plan.additions.len(), 1);
        assert_eq!(plan.additions[0].generation, 5);
        assert_eq!(plan.additions[0].reason, SpawnReason::Replugged);
        assert!(plan.retirements.is_empty());
    }

    #[test]
    fn geometry_or_command_change_retires_and_spawns_next_generation() {
        let old = display("DP-1", 1920, &["gsr", "-w", "DP-1", "-f", "60"]);
        let new_mode = display("DP-1", 2560, &["gsr", "-w", "DP-1", "-f", "60"]);
        let plan = reconcile(
            std::slice::from_ref(&new_mode),
            &[current(&old, "old-1", 2)],
            10.,
        );
        assert_eq!(plan.retirements.len(), 1);
        assert_eq!(plan.retirements[0].id, "old-1");
        assert_eq!(plan.retirements[0].reason, RetireReason::Superseded);
        assert_eq!(plan.additions.len(), 1);
        assert_eq!(plan.additions[0].generation, 3);
        assert_eq!(plan.additions[0].reason, SpawnReason::Changed);
        assert_ne!(old.fingerprint, new_mode.fingerprint);

        let command_changed = display("DP-1", 1920, &["gsr", "-w", "DP-1", "-f", "30"]);
        let plan = reconcile(
            std::slice::from_ref(&command_changed),
            &[current(&old, "old-2", 1)],
            10.,
        );
        assert_eq!(plan.additions[0].reason, SpawnReason::Changed);
        assert_eq!(plan.additions[0].generation, 2);
    }

    #[test]
    fn failing_probe_never_retires_the_session() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let live = current(&wanted, "track-1", 1);
        let snapshot = snapshot_from_video_plan(&json!({
            "ok": false,
            "transient": true,
            "error": "GSR display discovery timed out",
        }));
        let plan = reconcile_snapshot(&snapshot, std::slice::from_ref(&live), 10.);
        assert_eq!(plan.status, PlanStatus::ProbeFailed);
        assert!(plan.retirements.is_empty());
        assert!(plan.additions.is_empty());

        let malformed = snapshot_from_video_plan(&json!({
            "ok": true,
            "tracks": [{"key":"display:DP-1","meta":{"type":"audio","name":"speakers"}}],
        }));
        assert!(matches!(malformed, InventorySnapshot::Failed { .. }));
        let plan = reconcile_snapshot(&malformed, std::slice::from_ref(&live), 10.);
        assert!(plan.retirements.is_empty());
    }

    #[test]
    fn successful_empty_snapshot_and_static_source_are_distinct() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let live = current(&wanted, "track-1", 1);
        let empty = reconcile_snapshot(
            &InventorySnapshot::Empty,
            std::slice::from_ref(&live),
            10.,
        );
        assert_eq!(empty.retirements.len(), 1);
        assert_eq!(empty.retirements[0].reason, RetireReason::Disconnected);

        let static_plan = reconcile_snapshot(
            &InventorySnapshot::Static,
            std::slice::from_ref(&live),
            10.,
        );
        assert_eq!(static_plan.status, PlanStatus::Static);
        assert!(static_plan.retirements.is_empty());
    }

    #[test]
    fn dead_process_respects_exponential_backoff_then_retries() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let mut dead = current(&wanted, "dead-1", 2);
        dead.alive = false;
        dead.closed = false;
        dead.failures = 2;
        dead.last_attempt = Some(100.);

        let waiting = reconcile(std::slice::from_ref(&wanted), std::slice::from_ref(&dead), 101.);
        assert!(waiting.additions.is_empty());
        assert_eq!(waiting.deferred.len(), 1);
        assert_eq!(waiting.deferred[0].retry_at, 104.);

        let due = reconcile(std::slice::from_ref(&wanted), std::slice::from_ref(&dead), 104.);
        assert!(due.deferred.is_empty());
        assert_eq!(due.additions.len(), 1);
        assert_eq!(due.additions[0].generation, 3);
        assert_eq!(due.additions[0].reason, SpawnReason::Retry);
        assert_eq!(due.additions[0].failures, 2);
        assert_eq!(due.retirements.len(), 1);
        assert_eq!(due.retirements[0].reason, RetireReason::Failed);
    }

    #[test]
    fn retry_delay_is_bounded() {
        assert_eq!(retry_delay(0), RETRY_BASE);
        assert_eq!(retry_delay(1), RETRY_BASE);
        assert_eq!(retry_delay(2), 4.);
        assert_eq!(retry_delay(99), RETRY_MAX);
    }

    #[test]
    fn strict_parsing_rejects_audio_duplicates_and_malformed_entries() {
        let cases = [
            json!({"meta":{"type":"audio","name":"speakers"}}),
            json!({"name":"DP-1","width":1920,"type":"audio"}),
            json!("not an object"),
            json!({"meta":{"type":"video"},"command":"not-an-array"}),
        ];
        for case in cases {
            assert!(DisplaySpec::from_value(&case).is_err(), "{case}");
        }
        assert!(parse_display_list(&json!([
            {"name":"DP-1","width":1920},
            {"name":"DP-1","width":1280}
        ]))
        .is_err());
        assert!(parse_display_list(&json!({"not":"an array"})).is_err());
    }

    #[test]
    fn current_audio_tracks_are_skipped_without_affecting_video_actions() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let tracks = parse_current_list(&json!([
            {"id":"audio-1","key":"pulse:default","meta":{"type":"audio","name":"Speakers"},"alive":true},
            {
                "id":"video-1","key":"display:DP-1",
                "meta":{"type":"video","name":"DP-1","width":1920,"height":1080,"x":0,
                        "logical_width":1920,"logical_height":1080,
                        "hotplug_fingerprint":wanted.fingerprint,"hotplug_generation":1},
                "alive":true,"closed":false
            }
        ]))
        .unwrap();
        assert_eq!(tracks.len(), 1);
        let plan = reconcile(&[wanted], &tracks, 10.);
        assert!(plan.additions.is_empty());
        assert!(plan.retirements.is_empty());
        assert_eq!(plan.unchanged, vec!["display:DP-1"]);
    }

    #[test]
    fn labels_do_not_change_the_fingerprint_but_commands_do() {
        let a = DisplaySpec::from_value(&json!({
            "name":"DP-1","width":1920,"height":1080,"label":"Desk","command":["gsr","-w","DP-1"]
        }))
        .unwrap();
        let b = DisplaySpec::from_value(&json!({
            "name":"DP-1","width":1920,"height":1080,"label":"Renamed","command":["gsr","-w","DP-1"]
        }))
        .unwrap();
        let c = DisplaySpec::from_value(&json!({
            "name":"DP-1","width":1920,"height":1080,"label":"Desk","command":["gsr","-w","DP-1","-f","30"]
        }))
        .unwrap();
        assert_eq!(a.fingerprint, b.fingerprint);
        assert_ne!(a.fingerprint, c.fingerprint);
    }

    #[test]
    fn stamped_meta_round_trips_through_current_parsing() {
        let wanted = display("DP-1", 1920, &["gsr", "-w", "DP-1"]);
        let stamped = wanted.stamped_meta(7);
        let tracks = parse_current_list(&json!([{
            "id":"video-7","key":wanted.key,"meta":stamped,"alive":true,"closed":false
        }]))
        .unwrap();
        assert_eq!(tracks[0].fingerprint, wanted.fingerprint);
        assert_eq!(tracks[0].generation, 7);
        assert!(reconcile(&[wanted], &tracks, 1.).additions.is_empty());
    }
}