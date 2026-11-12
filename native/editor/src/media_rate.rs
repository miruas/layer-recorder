// SPDX-License-Identifier: GPL-3.0-only
//! Best-effort source-rate discovery. Run only on the document-loading worker.
use serde_json::Value;
use std::{
    collections::HashMap,
    fs,
    io::{self, Read},
    os::{fd::AsRawFd, unix::process::CommandExt},
    path::{Component, Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const OUTPUT_LIMIT: usize = 16 * 1024;
#[derive(Clone, Debug)]
struct Rates {
    average: Option<String>,
    nominal: Option<String>,
}

/// Enrich missing source-rate metadata without changing capture or model FPS.
/// Failures are nonfatal diagnostics. At most 2 seconds per probe and 3 seconds
/// for the whole call; repeated source files share a per-call probe result.
pub fn enrich_missing_rates(project: &mut Value, project_path: &Path) -> Vec<String> {
    enrich_with(
        project,
        project_path,
        Path::new("ffprobe"),
        Duration::from_secs(2),
        Duration::from_secs(3),
    )
}

fn enrich_with(
    project: &mut Value,
    project_path: &Path,
    program: &Path,
    per_probe: Duration,
    total: Duration,
) -> Vec<String> {
    let deadline = Instant::now() + total;
    let mut diagnostics = Vec::new();
    let Some(sources) = project.get_mut("sources").and_then(Value::as_array_mut) else {
        return diagnostics;
    };
    let parent = project_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let root = match parent.canonicalize() {
        Ok(root) => root,
        Err(_) => {
            return vec!["Source frame-rate discovery: project folder is unavailable.".into()];
        }
    };
    let mut cache: HashMap<PathBuf, Result<Rates, String>> = HashMap::new();
    for source in sources {
        if Instant::now() >= deadline {
            diagnostics
                .push("Source frame-rate discovery reached its 3-second time budget.".into());
            break;
        }
        if source["type"] != "video" || has_rate(source) {
            continue;
        }
        // Existing fields, even malformed ones, belong to the project. Never
        // replace them; an absent alternative rate may still be discovered.
        if source.get("avg_frame_rate").is_some() && source.get("r_frame_rate").is_some() {
            continue;
        }
        let Some(relative) = source.get("file").and_then(Value::as_str) else {
            continue;
        };
        let path = match safe_source(&root, relative) {
            Ok(path) => path,
            Err(message) => {
                diagnostics.push(message);
                continue;
            }
        };
        let rates = cache.entry(path.clone()).or_insert_with(|| {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("Source frame-rate discovery exhausted its time budget.".into());
            }
            probe(program, &path, per_probe.min(remaining))
        });
        match rates {
            Ok(rates) => {
                if let Some(object) = source.as_object_mut() {
                    for (key, value) in [
                        ("avg_frame_rate", &rates.average),
                        ("r_frame_rate", &rates.nominal),
                    ] {
                        if !object.contains_key(key) {
                            if let Some(value) = value {
                                object.insert(key.into(), Value::String(value.clone()));
                            }
                        }
                    }
                }
            }
            Err(message) => diagnostics.push(message.clone()),
        }
    }
    diagnostics
}

fn has_rate(source: &Value) -> bool {
    ["fps", "avg_frame_rate", "r_frame_rate"].iter().any(|key| {
        let value = &source[*key];
        value
            .as_f64()
            .filter(|n| n.is_finite() && *n > 0.0 && *n <= 1000.0)
            .is_some()
            || value.as_str().is_some_and(valid_rate)
    })
}
fn valid_rate(text: &str) -> bool {
    let value = if let Some((n, d)) = text.split_once('/') {
        match (n.parse::<f64>(), d.parse::<f64>()) {
            (Ok(n), Ok(d)) if d > 0.0 => n / d,
            _ => return false,
        }
    } else {
        match text.parse::<f64>() {
            Ok(n) => n,
            Err(_) => return false,
        }
    };
    value.is_finite() && value > 0.0 && value <= 1000.0
}
fn safe_source(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if relative.is_empty()
        || relative.contains("://")
        || path.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(
            "Source frame-rate discovery skipped a file outside the project folder.".into(),
        );
    }
    let path = root
        .join(path)
        .canonicalize()
        .map_err(|_| "Source frame-rate discovery skipped an unavailable file.".to_string())?;
    if !path.starts_with(root) {
        return Err(
            "Source frame-rate discovery skipped a file outside the project folder.".into(),
        );
    }
    if !fs::metadata(&path).map(|m| m.is_file()).unwrap_or(false) {
        return Err("Source frame-rate discovery accepts regular video files only.".into());
    }
    Ok(path)
}
fn stop(child: &mut Child) {
    // ffprobe normally has no children; the group also bounds a stuck wrapper.
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
}
fn drain(output: &mut impl Read, bytes: &mut Vec<u8>) -> Result<(), String> {
    let mut block = [0; 4096];
    loop {
        match output.read(&mut block) {
            Ok(0) => return Ok(()),
            Ok(count) => {
                if bytes.len() + count > OUTPUT_LIMIT {
                    return Err("Source frame-rate probe returned excessive output.".into());
                }
                bytes.extend_from_slice(&block[..count]);
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(()),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return Err("Could not read source frame-rate probe output.".into()),
        }
    }
}
fn probe(program: &Path, path: &Path, budget: Duration) -> Result<Rates, String> {
    let deadline = Instant::now() + budget;
    let mut child = Command::new(program)
        .args([
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "json",
        ])
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map_err(|_| "Could not start source frame-rate probe.".to_string())?;
    let mut output = child.stdout.take().unwrap();
    let flags = unsafe { libc::fcntl(output.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(output.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        stop(&mut child);
        return Err("Could not bound source frame-rate probe output.".into());
    }
    let mut bytes = Vec::new();
    loop {
        if let Err(message) = drain(&mut output, &mut bytes) {
            stop(&mut child);
            return Err(message);
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                // Drain available bytes without waiting on inherited pipe FDs.
                let result = drain(&mut output, &mut bytes);
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                result?;
                if !status.success() {
                    return Err("Source frame-rate probe could not read the video.".into());
                }
                break;
            }
            Ok(None) => (),
            Err(_) => {
                stop(&mut child);
                return Err("Source frame-rate probe could not be monitored.".into());
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            stop(&mut child);
            return Err("Source frame-rate probe timed out.".into());
        }
        thread::sleep(remaining.min(Duration::from_millis(10)));
    }
    let data: Value = serde_json::from_slice(&bytes)
        .map_err(|_| "Source frame-rate probe returned invalid JSON.".to_string())?;
    let stream = data["streams"]
        .as_array()
        .and_then(|streams| streams.first())
        .ok_or("Source frame-rate probe found no video stream.")?;
    let rate = |key: &str| {
        stream[key]
            .as_str()
            .filter(|text| valid_rate(text))
            .map(str::to_owned)
    };
    let rates = Rates {
        average: rate("avg_frame_rate"),
        nominal: rate("r_frame_rate"),
    };
    if rates.average.is_none() && rates.nominal.is_none() {
        return Err("Source frame-rate probe found no usable frame rate.".into());
    }
    Ok(rates)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::{PermissionsExt, symlink};
    fn helper(root: &Path, text: &str) -> PathBuf {
        let file = root.join("probe-helper");
        fs::write(&file, format!("#!/bin/sh\n{text}\n")).unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o700)).unwrap();
        file
    }
    fn video(root: &Path, name: &str) -> Value {
        fs::write(root.join(name), b"test fixture").unwrap();
        json!({"id":name,"type":"video","file":name,"custom":{"keep":true}})
    }
    #[test]
    fn actual_generated_60_and_120_fps_video_are_discovered() {
        if Command::new("ffmpeg")
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_err()
        {
            eprintln!("Skipping generated-media test: FFmpeg is unavailable.");
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let mut project = json!({"fps":60,"studio":{"fps":24},"custom":"keep","sources":[]});
        for fps in [60, 120] {
            let name = format!("source{fps}.mkv");
            assert!(
                Command::new("ffmpeg")
                    .args([
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        &format!("testsrc2=size=32x32:rate={fps}:duration=0.2"),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-threads",
                        "1"
                    ])
                    .arg(temp.path().join(&name))
                    .status()
                    .unwrap()
                    .success()
            );
            project["sources"]
                .as_array_mut()
                .unwrap()
                .push(json!({"id":name,"type":"video","file":name}));
        }
        let diagnostics = enrich_missing_rates(&mut project, &temp.path().join("project.katman"));
        assert!(diagnostics.is_empty(), "{diagnostics:?}");
        assert_eq!(project["sources"][0]["avg_frame_rate"], "60/1");
        assert_eq!(project["sources"][1]["avg_frame_rate"], "120/1");
        assert_eq!(project["sources"][0]["r_frame_rate"], "60/1");
        assert_eq!(project["sources"][1]["r_frame_rate"], "120/1");
        assert_eq!(project["fps"], 60);
        assert_eq!(project["studio"]["fps"], 24);
        assert_eq!(project["custom"], "keep");
    }
    #[test]
    fn caches_each_canonical_file_and_preserves_existing_metadata() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let source = video(root, "same.mkv");
        symlink(root.join("same.mkv"), root.join("alias.mkv")).unwrap();
        let program = helper(
            root,
            "echo call >> \"$(dirname \"$0\")/calls\"\nprintf '%s' '{\"streams\":[{\"avg_frame_rate\":\"120/1\",\"r_frame_rate\":\"120/1\"}]}'",
        );
        let preserved =
            json!({"type":"video","file":"same.mkv","fps":60,"avg_frame_rate":"60/1","custom":7});
        let mut project = json!({"sources":[source,{"type":"video","file":"alias.mkv"},preserved.clone(),{"type":"audio","file":"same.mkv"}]});
        assert!(
            enrich_with(
                &mut project,
                &root.join("project.katman"),
                &program,
                Duration::from_secs(1),
                Duration::from_secs(1)
            )
            .is_empty()
        );
        assert_eq!(
            fs::read_to_string(root.join("calls"))
                .unwrap()
                .lines()
                .count(),
            1
        );
        assert_eq!(project["sources"][2], preserved);
        assert_eq!(project["sources"][0]["custom"], json!({"keep":true}));
        assert_eq!(project["sources"][1]["avg_frame_rate"], "120/1");
        assert!(project["sources"][3].get("avg_frame_rate").is_none());
    }
    #[test]
    fn unsafe_paths_and_fifo_never_start_probe() {
        let temp = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let root = temp.path();
        fs::write(outside.path().join("external.mkv"), b"external").unwrap();
        symlink(outside.path().join("external.mkv"), root.join("escape.mkv")).unwrap();
        let fifo = root.join("pipe.mkv");
        let name = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let program = helper(root, "echo invoked > \"$(dirname \"$0\")/called\"");
        let mut project = json!({"sources":[{"type":"video","file":"pipe.mkv"},{"type":"video","file":"escape.mkv"},{"type":"video","file":"../external.mkv"},{"type":"video","file":"https://example.invalid/movie"},{"type":"video","file":outside.path().join("external.mkv").to_string_lossy()}]});
        let original = project.clone();
        let started = Instant::now();
        let diagnostics = enrich_with(
            &mut project,
            &root.join("project.katman"),
            &program,
            Duration::from_millis(100),
            Duration::from_secs(1),
        );
        assert_eq!(diagnostics.len(), 5);
        assert_eq!(project, original);
        assert!(!root.join("called").exists());
        assert!(started.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn malformed_and_failed_probes_are_nonfatal_and_do_not_overwrite() {
        for output in [
            "printf invalid",
            "printf '%s' '{\"streams\":[{\"avg_frame_rate\":\"0/0\",\"r_frame_rate\":\"nan\"}]}'",
            "exit 2",
        ] {
            let temp = tempfile::tempdir().unwrap();
            let root = temp.path();
            let source = video(root, "video.mkv");
            let program = helper(root, output);
            let mut project = json!({"sources":[source]});
            let original = project.clone();
            assert!(
                !enrich_with(
                    &mut project,
                    &root.join("project.katman"),
                    &program,
                    Duration::from_millis(200),
                    Duration::from_secs(1)
                )
                .is_empty()
            );
            assert_eq!(project, original);
        }
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let mut source = video(root, "video.mkv");
        source["avg_frame_rate"] = json!("malformed-but-preserved");
        let program = helper(
            root,
            "printf '%s' '{\"streams\":[{\"avg_frame_rate\":\"60/1\",\"r_frame_rate\":\"60/1\"}]}'",
        );
        let mut project = json!({"sources":[source]});
        assert!(
            enrich_with(
                &mut project,
                &root.join("project.katman"),
                &program,
                Duration::from_millis(200),
                Duration::from_secs(1)
            )
            .is_empty()
        );
        assert_eq!(
            project["sources"][0]["avg_frame_rate"],
            "malformed-but-preserved"
        );
        assert_eq!(project["sources"][0]["r_frame_rate"], "60/1");
    }
    #[test]
    fn blocked_process_and_total_probe_budget_are_bounded() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let sources: Vec<Value> = (0..5)
            .map(|i| video(root, &format!("video{i}.mkv")))
            .collect();
        let program = helper(
            root,
            "echo $$ >> \"$(dirname \"$0\")/calls\"\nexec /bin/sleep 10",
        );
        let mut project = json!({"sources":sources});
        let original = project.clone();
        let started = Instant::now();
        let diagnostics = enrich_with(
            &mut project,
            &root.join("project.katman"),
            &program,
            Duration::from_millis(70),
            Duration::from_millis(110),
        );
        assert!(started.elapsed() < Duration::from_millis(600));
        assert!(!diagnostics.is_empty());
        assert_eq!(project, original);
        let calls = fs::read_to_string(root.join("calls")).unwrap();
        assert!((1..=2).contains(&calls.lines().count()));
        for pid in calls.lines() {
            assert_eq!(
                unsafe { libc::kill(pid.parse::<i32>().unwrap(), 0) },
                -1,
                "probe must be reaped"
            );
        }
    }
    #[test]
    fn inherited_stdout_cannot_hold_up_a_completed_probe() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let source = video(root, "video.mkv");
        let program = helper(
            root,
            "/bin/sleep 10 &\nprintf '%s' '{\"streams\":[{\"avg_frame_rate\":\"60/1\"}]}'",
        );
        let mut project = json!({"sources":[source]});
        let started = Instant::now();
        assert!(
            enrich_with(
                &mut project,
                &root.join("project.katman"),
                &program,
                Duration::from_millis(200),
                Duration::from_secs(1)
            )
            .is_empty()
        );
        assert!(started.elapsed() < Duration::from_millis(600));
        assert_eq!(project["sources"][0]["avg_frame_rate"], "60/1");
    }
}
