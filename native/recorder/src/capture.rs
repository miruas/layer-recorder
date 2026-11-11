// SPDX-License-Identifier: GPL-3.0-only
//! GSR-only producers, bounded RAM replay, durable ordinary-recording spool and project snapshots.
use crate::{
    audio::{self, Monitor},
    buffer_storage::{self, MIN_FREE},
    platform::Platform,
    track::{Track, interval},
    util::{self, atomic_json, now},
};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    thread,
    time::Duration,
};
pub struct Recorder {
    pub running: bool,
    pub replay: bool,
    pub record_start: Option<f64>,
    pub origin: f64,
    pub tracks: Vec<Track>,
    pub errors: Vec<String>,
    pub last_project: Option<String>,
    pub buffer_limit: f64,
    root: Option<PathBuf>,
    archive: Option<PathBuf>,
    cache: PathBuf,
    platform: Platform,
    monitor: Option<Monitor>,
    audio_mode: String,
    warnings: Vec<String>,
    config: Value,
    program: String,
    revision: u64,
    identity: Value,
    last_identity: f64,
    last_spawn: BTreeMap<String, f64>,
}
impl Recorder {
    pub fn new(root: &Path, cache: PathBuf) -> Result<Self> {
        let mut platform = Platform::new(root)?;
        let config = platform.call("settings")?;
        Ok(Self {
            running: false,
            replay: false,
            record_start: None,
            origin: 0.,
            tracks: vec![],
            errors: vec![],
            last_project: None,
            buffer_limit: 300.,
            root: None,
            archive: None,
            cache,
            platform,
            monitor: None,
            audio_mode: "none".into(),
            warnings: vec![],
            config,
            program: String::new(),
            revision: u64::MAX,
            identity: json!({"application":"Desktop","shortcut_error":""}),
            last_identity: 0.,
            last_spawn: BTreeMap::new(),
        })
    }
    pub fn autostart(&self) -> bool {
        self.config["auto_replay"] == true
    }
    pub fn status(&mut self) -> Value {
        let elapsed = if self.running {
            now() - self.origin
        } else {
            0.
        };
        let sources: Vec<Value> = self
            .tracks
            .iter_mut()
            .map(|t| {
                let active = t.alive();
                let mut meta = t.meta.clone();
                meta["id"] = json!(t.id);
                meta["active"] = json!(active);
                meta
            })
            .collect();
        json!({"engine":"rust-gsr","protocol_version":1,"running":self.running,"replay":self.replay,"recording":self.record_start.is_some(),"buffer_storage":"ram","buffer_directory":self.root,"buffer_seconds":elapsed.min(self.buffer_limit).max(0.),"record_seconds":self.record_start.map(|start|(elapsed-start).max(0.)).unwrap_or(0.),"sources":sources,"audio_mode":self.audio_mode,"warnings":self.warnings,"errors":self.errors.iter().rev().take(10).rev().collect::<Vec<_>>(),"last_project":self.last_project,"cache_bytes":self.tracks.iter().map(Track::cache_bytes).sum::<u64>(),"shortcut_error":self.identity["shortcut_error"],"integrations":self.identity["integrations"]})
    }
    pub(crate) fn error(&mut self, error: String) {
        if self.errors.last() != Some(&error) {
            self.errors.push(error);
        }
        if self.errors.len() > 64 {
            self.errors.drain(..self.errors.len() - 64);
        }
    }
    fn update_identity(&mut self) {
        if now() - self.last_identity >= 1. {
            match self.platform.call("identity") {
                Ok(value) => self.identity = value,
                Err(e) => self.error(format!("Application identity: {e}")),
            };
            self.last_identity = now();
        }
    }
    pub fn start(&mut self, mode: &str) -> Result<()> {
        if self.running {
            return Ok(());
        }
        let plan = self.platform.call(if mode == "record" { "plan-record" } else { "plan" })?;
        self.config = plan["config"].clone();
        self.program = plan["program"].as_str().context("GSR program")?.into();
        self.buffer_limit = self.config["buffer"].as_f64().context("Buffer duration")?;
        self.errors.clear();
        self.audio_mode = plan["audio_mode"].as_str().unwrap_or("pipewire").into();
        self.warnings = plan["warnings"].as_array().map(|items| items.iter().filter_map(|v|v.as_str().map(str::to_string)).collect()).unwrap_or_default();
        self.monitor = if self.audio_mode == "pipewire" { Some(Monitor::new()?) } else { None };
        self.tracks.clear();
        self.last_spawn.clear();
        self.root = Some(buffer_storage::create()?);
        self.archive = Some(self.cache.join(format!(
                "recording-{}",
                self.root
                    .as_ref()
                    .unwrap()
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
            )));
        self.origin = now();
        let result = (|| -> Result<()> {
            for p in plan["tracks"].as_array().context("Capture plan tracks")? {
                let command: Vec<String> = p["command"]
                    .as_array()
                    .context("Capture command")?
                    .iter()
                    .map(|v| v.as_str().unwrap_or("").to_string())
                    .collect();
                let mut track = Track::spawn(
                    self.root.as_ref().unwrap(),
                    self.origin,
                    p["key"].as_str().context("Track key")?.into(),
                    p["meta"].clone(),
                    &command,
                    p["selector"].as_str().unwrap_or("").into(),
                )?;
                if track.meta["type"] == "audio" {
                    interval(&mut track.activity, true, track.offset);
                    interval(&mut track.presence, true, track.offset);
                }
                self.tracks.push(track);
            }
            if self.tracks.is_empty() {
                bail!("No GSR sources were selected.");
            }
            // Portal selection is interactive; allow time for the user to grant consent.
            let timeout = if self.tracks.iter().any(|t| t.meta["name"] == "portal") {
                120.
            } else {
                10.
            };
            let deadline = now() + timeout;
            loop {
                for t in self.tracks.iter_mut().filter(|t| t.meta["type"] == "video") {
                    if !t.alive() {
                        bail!("{}", t.error());
                    }
                }
                if self.tracks.iter().filter(|t|t.meta["type"] == "video").all(|t| {
                    t.path.join("%08d.mkv.ts").exists() && t.path.join("00000000.mkv").exists()
                }) {
                    break;
                }
                if now() > deadline {
                    bail!(
                        "GSR display capture did not start in time. Check capture.log or the portal consent dialog."
                    );
                }
                thread::sleep(Duration::from_millis(50));
            }
            self.running = true;
            self.revision = u64::MAX;
            self.discover_audio()?;
            self.update_identity();
            self.write_session()?;
            Ok(())
        })();
        if let Err(error) = result {
            let _ = self.stop(true);
            return Err(error);
        }
        Ok(())
    }
    fn discover_audio(&mut self) -> Result<()> {
        let Some(monitor) = &self.monitor else { return Ok(()); };
        let (groups, revision, error) = {
            let graph = monitor.state.lock().unwrap();
            (
                audio::groups(&graph.nodes),
                graph.revision,
                graph.error.clone(),
            )
        };
        if !error.is_empty() {
            self.error(error);
        }
        if revision == self.revision {
            return Ok(());
        }
        self.revision = revision;
        let elapsed = now() - self.origin;
        // Retire only our encoders; GSR's passive links never replace speaker routes.
        for t in self
            .tracks
            .iter_mut()
            .filter(|t| t.meta["type"] == "audio" && !t.closed)
        {
            let group = groups
                .iter()
                .find(|g| g.key == t.key && g.selector == t.selector);
            interval(&mut t.activity, group.is_some_and(|g| g.active), elapsed);
            interval(&mut t.presence, group.is_some(), elapsed);
            if group.is_none() || !t.alive() {
                t.stop()?;
            }
        }
        for g in groups {
            if self
                .tracks
                .iter_mut()
                .any(|t| t.key == g.key && t.selector == g.selector && t.alive())
            {
                continue;
            }
            if self
                .last_spawn
                .get(&g.key)
                .is_some_and(|last| elapsed - last < 3.)
            {
                continue;
            }
            self.last_spawn.insert(g.key.clone(), elapsed);
            let command=vec![self.program.clone(),"-a".into(),g.selector.clone(),"-ac".into(),"opus".into(),"-ab".into(),"256".into(),"-c".into(),"segment".into(),"-ffmpeg-opts".into(),"segment_time=2;segment_format=matroska;segment_list={list};reset_timestamps=1;segment_format_options=cluster_time_limit=250:flush_packets=1".into(),"-write-first-frame-ts".into(),"yes".into(),"-o".into(),"{output}".into(),"-v".into(),"no".into()];
            let mut track = Track::spawn(
                self.root.as_ref().unwrap(),
                self.origin,
                g.key,
                g.meta,
                &command,
                g.selector,
            )?;
            interval(&mut track.activity, g.active, elapsed);
            interval(&mut track.presence, true, elapsed);
            self.tracks.push(track);
        }
        Ok(())
    }
    pub fn tick(&mut self) {
        self.update_identity();
        if !self.running {
            return;
        }
        let result = (|| -> Result<()> {
            self.discover_audio()?;
            let root = self.root.as_ref().context("RAM root")?;
            if buffer_storage::free_bytes(root)? < MIN_FREE
                || self.record_start.is_some()
                    && buffer_storage::free_bytes(&self.cache)? < MIN_FREE
            {
                self.error("Capture stopped safely because less than 512 MB remains in replay or recording storage.".into());
                self.replay = false;
                if self.record_start.is_some() {
                    self.toggle_record()?;
                } else {
                    self.stop(false)?;
                }
                return Ok(());
            }
            let elapsed = now() - self.origin;
            let cutoff = (elapsed - self.buffer_limit - 6.)
                .min(self.record_start.map(|s| s - 3.).unwrap_or(f64::INFINITY));
            let archive = self.archive.as_ref().context("Recording spool")?;
            let mut errors = vec![];
            for t in &mut self.tracks {
                t.prune(
                    cutoff,
                    self.record_start.map(|_| elapsed - self.buffer_limit - 6.),
                    archive,
                )?;
                if !t.closed && !t.alive() && !t.reported {
                    t.reported = true;
                    errors.push(t.error());
                }
            }
            for error in errors {
                self.error(error);
            }
            // Closed, expired generations do not accumulate in long-running replay sessions.
            self.tracks.retain(|t| !t.closed || !t.rows.is_empty());
            Ok(())
        })();
        if let Err(error) = result {
            self.error(format!("Source discovery: {error:#}"));
        }
    }
    fn write_session(&self) -> Result<()> {
        let sources: Vec<Value> = self
            .tracks
            .iter()
            .map(|t| {
                let mut v = t.meta.clone();
                v["id"] = json!(t.id);
                v["offset"] = json!(t.offset);
                v
            })
            .collect();
        atomic_json(
            &self
                .root
                .as_ref()
                .context("Session root")?
                .join("session.json"),
            &json!({"created":chrono::Local::now().to_rfc3339(),"sources":sources}),
        )
    }
    pub fn set_state(&mut self, kind: &str, enabled: bool) -> Result<Value> {
        let active = match kind {
            "replay" => self.replay,
            "record" => self.record_start.is_some(),
            _ => bail!("Invalid capture state."),
        };
        if active == enabled {
            return Ok(self.status());
        }
        match kind {
            "replay" => self.toggle_replay(),
            _ => self.toggle_record(),
        }
    }
    pub fn toggle_replay(&mut self) -> Result<Value> {
        if self.replay {
            self.replay = false;
            if self.record_start.is_none() {
                self.stop(true)?;
            }
        } else {
            self.start("replay")?;
            self.replay = true;
        }
        Ok(self.status())
    }
    pub fn toggle_record(&mut self) -> Result<Value> {
        if self.record_start.is_none() {
            self.start("record")?;
            self.record_start = Some(now() - self.origin);
            return Ok(self.status());
        }
        let end = now() - self.origin;
        let start = self.record_start.unwrap();
        if !self.replay {
            self.stop(false)?;
        }
        // On save failure, retain the recording marker and recoverable media.
        let project = self.snapshot(start, end, "recording")?;
        self.record_start = None;
        if !self.running {
            self.cleanup();
        }
        let mut status = self.status();
        status["saved"] = json!(project);
        Ok(status)
    }
    pub fn reload(&mut self) -> Result<Value> {
        if self.record_start.is_some() {
            bail!("Settings saved. Stop the ordinary recording before applying capture changes.");
        }
        let replay = self.replay;
        let saved = if self.running && replay && now() - self.origin >= 0.5 {
            Some(self.snapshot(
                0f64.max(now() - self.origin - self.buffer_limit),
                now() - self.origin,
                "replay",
            )?)
        } else {
            None
        };
        if self.running {
            self.stop(true)?;
            self.replay = false;
        }
        if replay {
            self.start("replay")?;
            self.replay = true;
        }
        let mut status = self.status();
        status["preserved_project"] = json!(saved);
        Ok(status)
    }
    pub fn save_replay(&mut self, seconds: u64) -> Result<Value> {
        if seconds == 0 || seconds as f64 > self.buffer_limit {
            bail!("Replay duration must fit inside the configured RAM buffer.");
        }
        if !self.running || !self.replay {
            bail!("Enable the replay buffer first.");
        }
        let end = now() - self.origin;
        Ok(json!(self.snapshot(
            (end - seconds as f64).max(0.),
            end,
            "replay"
        )?))
    }
    fn cleanup(&mut self) {
        if let Some(path) = &self.root {
            let _ = fs::remove_dir_all(path);
        }
        if let Some(path) = &self.archive {
            let _ = fs::remove_dir_all(path);
        }
    }
    pub fn stop(&mut self, clean: bool) -> Result<()> {
        for t in &mut self.tracks {
            t.signal();
        }
        let mut failure = None;
        for t in &mut self.tracks {
            if let Err(e) = t.stop() {
                failure = Some(e);
            }
        }
        self.running = false;
        self.monitor = None;
        if clean {
            self.cleanup();
        }
        if let Some(e) = failure {
            return Err(e);
        }
        Ok(())
    }
    pub fn shutdown(&mut self) -> Result<()> {
        if self.record_start.is_some() {
            self.replay = false;
            self.toggle_record()?;
        } else if self.running {
            self.stop(true)?;
        }
        Ok(())
    }
    pub fn snapshot(&mut self, start: f64, end: f64, kind: &str) -> Result<String> {
        if end - start < 0.5 {
            bail!("The recording is too short. Wait at least one second.");
        }
        self.update_identity();
        if self.running {
            let deadline = now() + 4.5;
            loop {
                let mut covered = true;
                for t in &mut self.tracks {
                    if !t.alive() || t.offset >= end {
                        continue;
                    }
                    let rows = t.segments(false)?;
                    if rows.last().is_none_or(|r| t.offset + r.end < end) {
                        covered = false;
                    }
                }
                if covered || now() > deadline {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
        let cfg = self.platform.call("settings")?;
        let library = cfg["library"].as_str().context("Recording folder")?;
        let library = if library == "~" || library.starts_with("~/") {
            PathBuf::from(std::env::var("HOME")?)
                .join(library.trim_start_matches('~').trim_start_matches('/'))
        } else {
            PathBuf::from(library)
        };
        let application = audio::label(self.identity["application"].as_str().unwrap_or("Desktop"));
        let application = if application.is_empty() {
            "Desktop".to_string()
        } else {
            application
        };
        let folder = library.join(&application).join(format!(
            "{}_{}",
            chrono::Local::now().format("%Y-%m-%d_%H-%M-%S"),
            &util::id()[..5]
        ));
        fs::create_dir_all(folder.join("media"))?;
        let staging = folder.join(".staging");
        fs::create_dir(&staging)?;
        let mut project = json!({"format":"katman","version":1,"id":util::id(),"name":application,"application":application,"kind":kind,"created":chrono::Local::now().to_rfc3339(),"duration":end-start,"sources":[],"sections":[],"warnings":self.errors.iter().chain(self.warnings.iter()).collect::<Vec<_>>()});
        for track in &mut self.tracks {
            if !track.active_in(start, end) {
                continue;
            }
            let outcome = save_track(track, start, end, &folder, &staging);
            match outcome {
                Ok(Some(item)) => {
                    let coverage = item["timeline_start"].as_f64().unwrap()
                        + item["media_duration"].as_f64().unwrap()
                        - item["trim_in"].as_f64().unwrap();
                    if coverage < end - start - 0.25 {
                        project["warnings"].as_array_mut().unwrap().push(json!(format!("{}: the final {:0.2} seconds could not be saved; missing video holds and missing audio is silent.",track.meta["name"].as_str().unwrap_or("Source"),end-start-coverage)));
                    }
                    project["sources"].as_array_mut().unwrap().push(item);
                }
                Ok(None) => (),
                Err(e) => project["warnings"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(format!(
                        "{}: the segment could not be saved: {e:#}",
                        track.meta["name"].as_str().unwrap_or("Source")
                    ))),
            }
        }
        let path = folder.join("project.katman");
        if !project["sources"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["type"] == "video")
        {
            atomic_json(&folder.join("recovery.json"), &project)?;
            bail!(
                "No display footage could be saved. Recoverable pieces remain at {}",
                folder.display()
            );
        }
        if let Err(e) = atomic_json(&path, &project) {
            let _ = atomic_json(&folder.join("recovery.json"), &project);
            return Err(e);
        }
        fs::remove_dir_all(staging)?;
        let path = path.to_string_lossy().into_owned();
        self.last_project = Some(path.clone());
        Ok(path)
    }
}
fn save_track(
    t: &mut Track,
    start: f64,
    end: f64,
    folder: &Path,
    staging: &Path,
) -> Result<Option<Value>> {
    let selected: Vec<_> = t
        .segments(true)?
        .into_iter()
        .filter(|r| r.path.exists() && t.offset + r.end > start && t.offset + r.start < end)
        .collect();
    if selected.is_empty() {
        return Ok(None);
    }
    let mut copies = vec![];
    for (i, row) in selected.iter().enumerate() {
        let copied = staging.join(format!("{}-{i}.mkv", t.id));
        fs::copy(&row.path, &copied)?;
        if fs::metadata(&copied)?.len() > 500 {
            copies.push((copied, row.start));
        }
    }
    if copies.is_empty() {
        return Ok(None);
    }
    let listing = staging.join(format!("{}.txt", t.id));
    fs::write(
        &listing,
        copies
            .iter()
            .map(|(p, _)| format!("file '{}'", p.to_string_lossy().replace('\'', "'\\''")))
            .collect::<Vec<_>>()
            .join("\n"),
    )?;
    let relative = format!("media/{}.mkv", t.id);
    let dest = folder.join(&relative);
    util::run(
        &[
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
        ]
        .map(str::to_string)
        .into_iter()
        .chain([listing.to_string_lossy().into_owned()])
        .chain(["-map", "0", "-c", "copy"].map(str::to_string))
        .chain([dest.to_string_lossy().into_owned()])
        .collect::<Vec<_>>(),
        90,
    )?;
    let info = util::probe(&dest)?;
    if info["streams"].as_array().is_none_or(|v| v.is_empty()) {
        bail!("The media segment is empty.");
    }
    let first = t.offset + copies[0].1;
    if t.meta["kind"] == "application"
        && !audio_content(&dest, (start - first).max(0.), end - start.max(first))
    {
        fs::remove_file(&dest)?;
        return Ok(None);
    }
    let mut item = t.meta.clone();
    item["id"] = json!(t.id);
    item["file"] = json!(relative);
    item["timeline_start"] = json!((first - start).max(0.));
    item["trim_in"] = json!((start - first).max(0.));
    item["media_duration"] = json!(
        info["format"]["duration"]
            .as_str()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.)
    );
    item["enabled"] = json!(true);
    item["gain"] = json!(1.);
    if item["type"] == "video" {
        let video = info["streams"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["codec_type"] == "video")
            .context("Missing video stream")?;
        item["width"] = video["width"].clone();
        item["height"] = video["height"].clone();
        if item["logical_width"] == 0 {
            item["logical_width"] = video["width"].clone();
            item["logical_height"] = video["height"].clone();
        }
    }
    if item["type"] == "audio" {
        if let Some(audio) = info["streams"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["codec_type"] == "audio")
        {
            item["audio_codec"] = audio["codec_name"].clone();
        }
    }
    Ok(Some(item))
}
fn audio_content(path: &Path, start: f64, span: f64) -> bool {
    let args = vec![
        "ffmpeg".into(),
        "-v".into(),
        "info".into(),
        "-nostats".into(),
        "-ss".into(),
        start.to_string(),
        "-i".into(),
        path.to_string_lossy().into_owned(),
        "-t".into(),
        span.max(0.01).to_string(),
        "-vn".into(),
        "-af".into(),
        "astats=metadata=0:reset=0:measure_perchannel=none:measure_overall=Peak_level".into(),
        "-f".into(),
        "null".into(),
        "-".into(),
    ];
    match util::output(&args, 30) {
        Ok(o) if o.ok => {
            let peaks: Vec<_> = o
                .stderr
                .lines()
                .filter_map(|l| l.split_once("Peak level dB:").map(|(_, v)| v.trim()))
                .collect();
            peaks.is_empty() || peaks.iter().any(|p| *p != "-inf")
        }
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn saved_media_combines_ram_and_spool_without_destroying_sources() {
        let ram = buffer_storage::create().unwrap();
        let disk = tempfile::tempdir().unwrap();
        let mut t = Track::spawn(
            &ram,
            now(),
            "qa".into(),
            json!({"type":"video","name":"Synthetic QA","width":64,"height":64}),
            &["sleep".into(), "60".into()],
            String::new(),
        )
        .unwrap();
        t.offset = 0.;
        let mut listing = String::new();
        for (i, color) in ["red", "blue"].iter().enumerate() {
            let path = t.path.join(format!("{i:08}.mkv"));
            let args = vec![
                "ffmpeg".into(),
                "-v".into(),
                "error".into(),
                "-f".into(),
                "lavfi".into(),
                "-i".into(),
                format!("color={color}:s=64x64:r=10:d=2"),
                "-c:v".into(),
                "libx264".into(),
                "-preset".into(),
                "ultrafast".into(),
                path.to_string_lossy().into_owned(),
            ];
            util::run(&args, 20).unwrap();
            listing.push_str(&format!("{},{},{}\n", path.display(), i * 2, i * 2 + 2));
        }
        fs::write(t.path.join("segments.csv"), listing).unwrap();
        t.prune(-1., Some(2.5), disk.path()).unwrap();
        assert_ne!(t.rows[0].path.parent(), t.rows[1].path.parent());
        let folder = disk.path().join("project");
        fs::create_dir_all(folder.join("media")).unwrap();
        let staging = folder.join(".staging");
        fs::create_dir(&staging).unwrap();
        let item = save_track(&mut t, 0., 4., &folder, &staging)
            .unwrap()
            .unwrap();
        assert!((item["media_duration"].as_f64().unwrap() - 4.).abs() < 0.1);
        let path = folder.join(item["file"].as_str().unwrap());
        let args = vec![
            "ffmpeg".into(),
            "-v".into(),
            "error".into(),
            "-ss".into(),
            "3".into(),
            "-i".into(),
            path.to_string_lossy().into_owned(),
            "-frames:v".into(),
            "1".into(),
            "-f".into(),
            "rawvideo".into(),
            "-pix_fmt".into(),
            "rgb24".into(),
            "-".into(),
        ];
        let frame = util::run(&args, 20).unwrap();
        assert!(frame[2] > 200 && frame[0] < 30);
        assert!(t.rows.iter().all(|r| r.path.exists()));
        t.stop().unwrap();
        fs::remove_dir_all(ram).unwrap();
    }
}
