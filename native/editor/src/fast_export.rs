// SPDX-License-Identifier: GPL-3.0-only
//! Conservative packet-copy video export. Planning is pure; I/O stays on job threads.
use crate::jobs::Control;
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
};

#[derive(Debug)]
pub struct Input {
    pub file: String,
    pub start: f64,
    pub seek: f64,
    pub length: f64,
    pub gain: f64,
}
#[derive(Debug)]
pub struct Plan {
    pub video: Input,
    pub audio: Vec<Input>,
    pub duration: f64,
}
fn n(v: &Value, k: &str, d: f64) -> f64 {
    v.get(k).map_or(d, |v| v.as_f64().unwrap_or(f64::NAN))
}
fn eq(a: f64, b: f64) -> bool {
    a.is_finite() && b.is_finite() && (a - b).abs() < 0.00001
}
fn neutral(c: &Value, video: bool) -> bool {
    let numeric = if video {
        vec![
            ("speed", 1.),
            ("opacity", 1.),
            ("rotation", 0.),
            ("crop_x", 0.),
            ("crop_y", 0.),
            ("crop_w", 1.),
            ("crop_h", 1.),
            ("brightness", 0.),
            ("contrast", 1.),
            ("saturation", 1.),
            ("gamma", 1.),
            ("temperature", 0.),
            ("tint", 0.),
            ("sharpness", 0.),
            ("border_width", 0.),
            ("fade_in", 0.),
            ("fade_out", 0.),
        ]
    } else {
        vec![("speed", 1.), ("fade_in", 0.), ("fade_out", 0.)]
    };
    numeric.iter().all(|(k, d)| eq(n(c, k, *d), *d))
        && [
            "reverse",
            "freeze",
            "blackout",
            "flip_x",
            "flip_y",
            "denoise",
            "normalize",
        ]
        .iter()
        .all(|k| c[*k].is_null() || c[*k] == false)
        && ["lut", "stabilization"]
            .iter()
            .all(|k| c[*k].as_str().unwrap_or("").is_empty())
        && (c["keys"].is_null() || c["keys"].is_object())
        && (c["speed_keys"].is_null() || c["speed_keys"].is_array())
        && c["keys"].as_object().is_none_or(|o| {
            o.values()
                .all(|v| v.as_array().is_some_and(|a| a.is_empty()))
        })
        && c["speed_keys"].as_array().is_none_or(|a| a.is_empty())
}
pub fn plan(project: &Value, model: &Value) -> Result<Plan> {
    if model["annotations"]
        .as_array()
        .is_some_and(|a| !a.is_empty())
        || model["subtitles"].as_array().is_some_and(|a| !a.is_empty())
    {
        bail!("Captions and drawing/privacy effects require rendering.");
    }
    let mut video = None;
    let mut audio = vec![];
    let mut end: f64 = 0.;
    for track in model["tracks"].as_array().context("No timeline tracks")? {
        if track["kind"] != "video" && track["kind"] != "audio" {
            bail!("Unknown track kind requires rendering.");
        }
        let isvideo = track["kind"] == "video";
        if track["mute"] == true || (isvideo && track["visible"] == false) {
            continue;
        }
        for c in track["clips"]
            .as_array()
            .context("Invalid timeline clips")?
        {
            if c["mute"] == true {
                continue;
            }
            if !neutral(c, isvideo) {
                bail!("Clip effects, speed changes or fades require rendering.");
            }
            let source = project["sources"]
                .as_array()
                .and_then(|rows| rows.iter().find(|s| s["id"] == c["source_id"]))
                .context("A clip source is missing")?;
            if (source["type"] == "video") != isvideo {
                bail!("Track and source types do not match.");
            }
            let input = Input {
                file: source["file"]
                    .as_str()
                    .context("Missing media path")?
                    .into(),
                start: n(c, "start", 0.),
                seek: n(c, "in", 0.),
                length: n(c, "length", 0.),
                gain: n(c, "gain", 1.),
            };
            if !input.start.is_finite()
                || input.start < 0.
                || !input.seek.is_finite()
                || input.seek < 0.
                || !input.length.is_finite()
                || input.length <= 0.
                || !input.gain.is_finite()
                || !(0.0..=2.0).contains(&input.gain)
            {
                bail!("Invalid clip timing or audio gain.");
            }
            end = end.max(input.start + input.length);
            if isvideo {
                if video.is_some() {
                    bail!("Compositing or joining video clips requires rendering.");
                }
                if source["media_duration"]
                    .as_f64()
                    .is_none_or(|d| !eq(d, input.length))
                {
                    bail!("A shortened video or missing source duration requires rendering.");
                }
                if !eq(input.start, 0.) || !eq(input.seek, 0.) {
                    bail!("A nonzero video cut requires rendering for frame-accurate output.");
                }
                if !eq(n(c, "x", 0.), 0.)
                    || !eq(n(c, "y", 0.), 0.)
                    || !eq(n(c, "w", 0.), n(source, "width", -1.))
                    || !eq(n(c, "h", 0.), n(source, "height", -1.))
                    || !eq(n(model, "width", 0.), n(source, "width", -1.))
                    || !eq(n(model, "height", 0.), n(source, "height", -1.))
                {
                    bail!("Resizing, positioning or multiple screens require rendering.");
                }
                let ext = Path::new(&input.file)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("")
                    .to_ascii_lowercase();
                if !["mkv", "mp4", "mov", "webm", "ts", "m4v"].contains(&ext.as_str()) {
                    bail!("This source format requires rendering.");
                }
                video = Some(input);
            } else {
                audio.push(input);
            }
        }
    }
    let video = video.context("Select one unchanged video source for original-quality export.")?;
    if !eq(crate::model::duration(model), video.length) {
        bail!("Timeline length differs from the original video; rendering is required.");
    }
    if !eq(end, video.length) {
        bail!("Audio extends beyond the video; rendering is required.");
    }
    if audio.len() > 64 {
        bail!("Too many audio clips for quick export.");
    }
    Ok(Plan {
        duration: video.length,
        video,
        audio,
    })
}
fn media(root: &Path, file: &str) -> Result<PathBuf> {
    let p = Path::new(file);
    if p.is_absolute()
        || p.components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        bail!("Source media must stay inside the recording folder.");
    }
    let p = root.join(p).canonicalize()?;
    if !p.starts_with(root) || !p.is_file() {
        bail!("Source media is missing or outside the recording folder.");
    }
    Ok(p)
}

/// Validate container display metadata without decoding any video frames.
fn inspect_source(
    file: &Path,
    model: &Value,
    expected_duration: f64,
    control: Arc<Control>,
) -> Result<()> {
    use std::{
        io,
        os::fd::AsRawFd,
        thread,
        time::{Duration, Instant},
    };
    control.check()?;
    let mut child=Command::new("ffprobe").args(["-v","error","-select_streams","v:0","-show_entries","stream=width,height,sample_aspect_ratio,duration:stream_tags=rotate:stream_side_data=rotation,displaymatrix:format=duration","-of","json"]).arg(file).process_group(0).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().context("Source validation needs FFprobe. Choose Render instead.")?;
    control.set_pid(child.id());
    struct Probe {
        child: std::process::Child,
        control: Arc<Control>,
    }
    impl Drop for Probe {
        fn drop(&mut self) {
            if !matches!(self.child.try_wait(), Ok(Some(_))) {
                unsafe {
                    libc::kill(-(self.child.id() as i32), libc::SIGKILL);
                }
                let _ = self.child.wait();
            }
            self.control.set_pid(0);
        }
    }
    let mut process = Probe {
        child,
        control: control.clone(),
    };
    let mut stdout = process.child.stdout.take().unwrap();
    let fd = stdout.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        bail!("Could not inspect source metadata.");
    }
    let mut bytes = vec![];
    let deadline = Instant::now() + Duration::from_secs(3);
    let status = loop {
        control.check()?;
        if Instant::now() > deadline {
            bail!("Source inspection timed out. Choose Render instead.");
        }
        let mut part = [0u8; 4096];
        loop {
            match stdout.read(&mut part) {
                Ok(0) => break,
                Ok(count) => {
                    bytes.extend_from_slice(&part[..count]);
                    if bytes.len() > 16384 {
                        bail!("Source metadata exceeds inspection limit.");
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) => return Err(e.into()),
            }
        }
        if let Some(status) = process.child.try_wait()? {
            let _ = stdout
                .by_ref()
                .take((16385usize.saturating_sub(bytes.len())) as u64)
                .read_to_end(&mut bytes);
            break status;
        }
        thread::sleep(Duration::from_millis(10));
    };
    if !status.success() || bytes.len() > 16384 {
        bail!("Source metadata is unavailable. Choose Render instead.");
    }
    let info: Value = serde_json::from_slice(&bytes)?;
    let stream = &info["streams"][0];
    if !eq(n(stream, "width", -1.), n(model, "width", 0.))
        || !eq(n(stream, "height", -1.), n(model, "height", 0.))
    {
        bail!("Actual source dimensions differ from the canvas. Choose Render instead.");
    }
    // Unspecified SAR is square-pixel default in GSR sources. Explicit non-square
    // aspect or a display transform must follow the compositor's normalization.
    if !matches!(
        stream["sample_aspect_ratio"].as_str(),
        None | Some("N/A" | "0:1" | "0:0" | "1:1")
    ) {
        bail!("Anamorphic video requires rendering to preserve canvas geometry.");
    }
    if stream["tags"]["rotate"]
        .as_str()
        .is_some_and(|r| r.parse::<f64>().ok() != Some(0.))
        || stream["side_data_list"].as_array().is_some_and(|rows| {
            rows.iter().any(|row| {
                row.get("displaymatrix").is_some()
                    || row["rotation"].as_f64().is_some_and(|r| r != 0.)
            })
        })
    {
        bail!("Video display rotation requires rendering.");
    }
    let actual = info["format"]["duration"]
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .context("Unknown source duration requires rendering.")?;
    if !actual.is_finite() || (actual - expected_duration).abs() > 0.001 {
        bail!("Actual source duration differs from the complete clip. Choose Render instead.");
    }
    Ok(())
}

pub fn run(request: &Value, control: Arc<Control>, progress: impl Fn(f64)) -> Result<Value> {
    let path =
        Path::new(request["path"].as_str().context("Missing project path")?).canonicalize()?;
    let metadata = fs::metadata(&path)?;
    if !metadata.is_file() || metadata.len() > 32 * 1024 * 1024 {
        bail!("Invalid project file.");
    }
    let project: Value = serde_json::from_slice(&fs::read(&path)?)?;
    if project["format"] != "katman" || project["version"] != 1 {
        bail!("Unsupported project format.");
    }
    let model = request.get("model").unwrap_or(&project["studio"]);
    let plan = plan(&project, model)?;
    let root = path.parent().unwrap();
    let video_file = media(root, &plan.video.file)?;
    inspect_source(&video_file, model, plan.duration, control.clone())?;
    let destination = PathBuf::from(
        request["output"]
            .as_str()
            .context("Choose an export destination")?,
    );
    if !destination.is_absolute() {
        bail!("Export destination must be an absolute path.");
    }
    let parent = destination.parent().context("Missing destination folder")?;
    fs::create_dir_all(parent)?;
    let parent = parent.canonicalize()?;
    let destination = parent.join(destination.file_name().context("Missing filename")?);
    if destination.exists() || destination == path || destination.starts_with(root.join("media")) {
        bail!("Choose a new destination outside the original media folder.");
    }
    let ext = destination
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !["mp4", "mkv", "mov"].contains(&ext.as_str()) {
        bail!("Original-quality export supports MP4, MOV and MKV.");
    }
    let temp = tempfile::Builder::new()
        .prefix(".layer-quick-")
        .suffix(&format!(".{ext}"))
        .tempfile_in(&parent)?;
    let errors = tempfile::tempfile()?;
    let mut cmd = Command::new("ffmpeg");
    cmd.args(["-hide_banner", "-v", "error", "-nostdin", "-y", "-i"])
        .arg(&video_file);
    for input in &plan.audio {
        cmd.args([
            "-ss",
            &format!("{:.7}", input.seek),
            "-t",
            &format!("{:.7}", input.length),
            "-i",
        ])
        .arg(media(root, &input.file)?);
    }
    cmd.args(["-map", "0:v:0", "-c:v", "copy"]);
    if plan.audio.is_empty() {
        cmd.arg("-an");
    } else {
        let mut filters = vec![];
        let mut labels = String::new();
        for (i, input) in plan.audio.iter().enumerate() {
            let idx = i + 1;
            filters.push(format!("[{idx}:a:0]asetpts=PTS-STARTPTS,volume={:.7},adelay={}:all=1,apad,atrim=duration={:.7}[a{idx}]",input.gain,(input.start*1000.).round()as u64,plan.duration));
            labels.push_str(&format!("[a{idx}]"));
        }
        filters.push(format!("{labels}amix=inputs={}:normalize=0:duration=longest,alimiter=limit=0.95:latency=1[aout]",plan.audio.len()));
        cmd.args([
            "-filter_complex_threads",
            "2",
            "-filter_complex",
            &filters.join(";"),
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
        ]);
    }
    // Do not map container-attached audio: Layer audio tracks are independent and
    // selected explicitly. Duration end uses packet boundaries, never -ss cuts.
    cmd.args([
        "-t",
        &format!("{:.7}", plan.duration),
        "-map_metadata",
        "-1",
    ]);
    if ext == "mp4" || ext == "mov" {
        cmd.args(["-movflags", "+faststart"]);
    }
    cmd.args(["-progress", "pipe:1", "-stats_period", "0.2"])
        .arg(temp.path())
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::from(errors.try_clone()?));
    control.check()?;
    let mut child = cmd.spawn().context("Could not start packet-copy export")?;
    control.set_pid(child.id());
    struct Guard<'a> {
        child: &'a mut std::process::Child,
        control: Arc<Control>,
    }
    impl Drop for Guard<'_> {
        fn drop(&mut self) {
            if !matches!(self.child.try_wait(), Ok(Some(_))) {
                unsafe {
                    libc::kill(-(self.child.id() as i32), libc::SIGKILL);
                }
                let _ = self.child.wait();
            }
            self.control.set_pid(0);
        }
    }
    let guard = Guard {
        child: &mut child,
        control: control.clone(),
    };
    control.check()?;
    for line in BufReader::new(guard.child.stdout.take().unwrap()).lines() {
        let line = line?;
        control.check()?;
        if let Some(v) = line
            .strip_prefix("out_time_us=")
            .and_then(|v| v.parse::<f64>().ok())
        {
            progress((v / 1e6 / plan.duration).clamp(0., 0.995));
        }
    }
    let status = guard.child.wait()?;
    control.check()?;
    if !status.success() {
        let mut error = String::new();
        let mut tail = errors;
        use std::io::{Seek, SeekFrom};
        tail.seek(SeekFrom::End(-(tail.metadata()?.len().min(3500) as i64)))?;
        tail.read_to_string(&mut error)?;
        bail!(
            "Original-quality export failed. Choose Render if the source codec is incompatible with the container. {error}"
        );
    }
    if temp.as_file().metadata()?.len() == 0 {
        bail!("Export produced no media.");
    }
    control.check()?;
    fs::hard_link(temp.path(), &destination)
        .context("Destination already exists or cannot be created")?;
    progress(1.);
    Ok(
        json!({"file":destination,"method":"video_stream_copy","video_reencoded":false,"audio_mixed":!plan.audio.is_empty()}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Value, Value) {
        (
            json!({"sources":[{"id":"v","type":"video","file":"media/source.mkv","width":640,"height":360,"media_duration":2}]}),
            json!({"width":640,"height":360,"tracks":[{"kind":"video","clips":[{"source_id":"v","start":0,"in":0,"length":2,"x":0,"y":0,"w":640,"h":360}]}]}),
        )
    }
    #[test]
    fn accepts_only_identity_video() {
        let (p, m) = fixture();
        assert!(plan(&p, &m).is_ok());
        for (k, v) in [
            ("opacity", json!(0.5)),
            ("in", json!(0.1)),
            ("rotation", json!(10)),
            ("blackout", json!(true)),
            ("crop_w", json!(0.8)),
        ] {
            let mut changed = m.clone();
            changed["tracks"][0]["clips"][0][k] = v;
            assert!(plan(&p, &changed).is_err(), "{k}");
        }
        let mut changed = m.clone();
        changed["annotations"] = json!([{"kind":"redact"}]);
        assert!(plan(&p, &changed).is_err());
    }
    #[test]
    fn refuses_compositing_and_changed_canvas() {
        let (p, mut m) = fixture();
        let extra = m["tracks"][0]["clips"][0].clone();
        m["tracks"][0]["clips"].as_array_mut().unwrap().push(extra);
        assert!(plan(&p, &m).is_err());
        let (p, mut m) = fixture();
        m["width"] = json!(1280);
        assert!(plan(&p, &m).is_err());
    }
    #[test]
    fn excludes_hidden_tracks_but_retains_audio_timing() {
        let (mut p, mut m) = fixture();
        p["sources"]
            .as_array_mut()
            .unwrap()
            .push(json!({"id":"a","type":"audio","file":"media/audio.mkv"}));
        m["tracks"].as_array_mut().unwrap().push(json!({"kind":"audio","clips":[{"source_id":"a","start":0.2,"in":0.3,"length":1.5,"gain":0.7}]}));
        let result = plan(&p, &m).unwrap();
        assert_eq!(result.audio.len(), 1);
        assert_eq!(result.audio[0].start, 0.2);
        m["tracks"][1]["mute"] = json!(true);
        assert!(plan(&p, &m).unwrap().audio.is_empty());
    }
    #[test]
    fn hidden_or_muted_tail_still_preserves_canonical_timeline_duration() {
        let (mut p, mut m) = fixture();
        p["sources"]
            .as_array_mut()
            .unwrap()
            .push(json!({"id":"a","type":"audio","file":"media/audio.mkv"}));
        m["tracks"].as_array_mut().unwrap().push(json!({"kind":"audio","mute":true,"clips":[{"source_id":"a","start":0,"in":0,"length":5}]}));
        assert!(
            plan(&p, &m)
                .unwrap_err()
                .to_string()
                .contains("Timeline length")
        );
    }
    #[test]
    fn real_packet_copy_keeps_video_samples_and_mixes_audio() {
        if Command::new("ffmpeg").arg("-version").output().is_err() {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let media_dir = dir.path().join("media");
        fs::create_dir(&media_dir).unwrap();
        let file = media_dir.join("source.mkv");
        let status = Command::new("ffmpeg")
            .args([
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=640x360:r=25:d=1",
                "-c:v",
                "ffv1",
                "-threads",
                "1",
            ])
            .arg(&file)
            .status()
            .unwrap();
        assert!(status.success());
        let audio = media_dir.join("tone.wav");
        assert!(
            Command::new("ffmpeg")
                .args([
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1"
                ])
                .arg(&audio)
                .status()
                .unwrap()
                .success()
        );
        let (mut p, mut m) = fixture();
        m["fps"] = json!(25);
        m["tracks"][0]["clips"][0]["length"] = json!(1);
        p["sources"][0]["media_duration"] = json!(1);
        p["sources"]
            .as_array_mut()
            .unwrap()
            .push(json!({"id":"a","type":"audio","file":"media/tone.wav"}));
        m["tracks"]
            .as_array_mut()
            .unwrap()
            .push(json!({"kind":"audio","clips":[{"source_id":"a","start":0,"in":0,"length":1}]}));
        p["format"] = json!("katman");
        p["version"] = json!(1);
        p["studio"] = m.clone();
        let path = dir.path().join("project.katman");
        fs::write(&path, serde_json::to_vec(&p).unwrap()).unwrap();
        let dest = dir.path().join("output.mkv");
        let request = json!({"path":path,"model":m,"output":dest});
        let data = run(&request, Control::standalone(), |_| {}).unwrap();
        assert_eq!(data["video_reencoded"], false);
        assert_eq!(data["audio_mixed"], true);
        fn hashes(path: &Path) -> Vec<String> {
            let r = Command::new("ffprobe")
                .args([
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_data_hash",
                    "sha256",
                    "-show_entries",
                    "packet=data_hash",
                    "-of",
                    "csv=p=0",
                ])
                .arg(path)
                .output()
                .unwrap();
            assert!(r.status.success());
            String::from_utf8(r.stdout)
                .unwrap()
                .lines()
                .map(str::to_owned)
                .collect()
        }
        assert_eq!(hashes(&file), hashes(&dest));
        assert!(run(&request, Control::standalone(), |_| {}).is_err());
        let v: Value = serde_json::from_slice(
            &Command::new("ffprobe")
                .args(["-v", "error", "-show_streams", "-of", "json"])
                .arg(&dest)
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap();
        assert!(
            v["streams"]
                .as_array()
                .unwrap()
                .iter()
                .any(|s| s["codec_name"] == "aac")
        );
        let sar = media_dir.join("sar.mkv");
        assert!(
            Command::new("ffmpeg")
                .args(["-v", "error", "-i"])
                .arg(&file)
                .args(["-vf", "setsar=2", "-c:v", "ffv1", "-threads", "1"])
                .arg(&sar)
                .status()
                .unwrap()
                .success()
        );
        let mut sar_project = p.clone();
        sar_project["sources"][0]["file"] = json!("media/sar.mkv");
        let sar_path = dir.path().join("sar.katman");
        fs::write(&sar_path, serde_json::to_vec(&sar_project).unwrap()).unwrap();
        let sar_output = dir.path().join("sar-output.mkv");
        let mut sar_request = request.clone();
        sar_request["path"] = json!(sar_path);
        sar_request["output"] = json!(sar_output);
        assert!(
            run(&sar_request, Control::standalone(), |_| {})
                .unwrap_err()
                .to_string()
                .contains("Anamorphic")
        );
        assert!(!sar_output.exists());
        let blocked = dir.path().join("blocked.mkv");
        let mut cancelled = request.clone();
        cancelled["output"] = json!(blocked);
        let control = Control::standalone();
        control.cancel_work();
        assert!(run(&cancelled, control, |_| {}).is_err());
        assert!(!blocked.exists());
        assert!(!fs::read_dir(dir.path()).unwrap().any(|e| {
            e.unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".layer-quick-")
        }));
    }
}

/// Headless fast export: no window, Python worker or compositor initialization.
pub fn cli() -> Option<Result<()>> {
    let args: Vec<String> = std::env::args().collect();
    let index = args.iter().position(|s| s == "--quick-export")?;
    Some((|| {
        let path = args
            .get(index + 1)
            .context("--quick-export requires a project path")?;
        let output = args
            .windows(2)
            .find(|a| a[0] == "--output")
            .map(|a| a[1].as_str())
            .context("--output is required")?;
        let doc = crate::model::Document::load(Path::new(path))?;
        let output = if Path::new(output).is_absolute() {
            PathBuf::from(output)
        } else {
            std::env::current_dir()?.join(output)
        };
        static CANCEL: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        extern "C" fn stop(_: i32) {
            CANCEL.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        unsafe {
            libc::signal(libc::SIGTERM, stop as *const () as libc::sighandler_t);
            libc::signal(libc::SIGINT, stop as *const () as libc::sighandler_t);
        }
        let control = Control::standalone();
        let weak = Arc::downgrade(&control);
        std::thread::spawn(move || {
            while let Some(control) = weak.upgrade() {
                if CANCEL.load(std::sync::atomic::Ordering::SeqCst) {
                    control.cancel_work();
                    break;
                }
                drop(control);
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
        });
        let result = run(
            &json!({"path":doc.path,"model":doc.model,"output":output}),
            control,
            |_| {},
        )?;
        println!("{result}");
        Ok(())
    })())
}
