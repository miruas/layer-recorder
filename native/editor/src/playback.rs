// SPDX-License-Identifier: GPL-3.0-only
//! Source-rate playback with bounded latest-frame delivery and repaint notification.
use anyhow::{Context, Result, bail};
use eframe::egui;
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::process::CommandExt,
    path::Path,
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
pub struct Frame {
    pub rgba: Vec<u8>,
    pub width: usize,
    pub height: usize,
    pub time: f64,
}
/// A stalled UI replaces its pending frame rather than accumulating latency.
#[derive(Clone, Default)]
pub struct LatestFrames(Arc<Mutex<Option<Frame>>>);
impl LatestFrames {
    fn publish(&self, frame: Frame) {
        *self.0.lock().unwrap_or_else(|e| e.into_inner()) = Some(frame);
    }
    pub fn try_iter(&self) -> std::option::IntoIter<Frame> {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
            .into_iter()
    }
}
pub struct Playback {
    pub frames: LatestFrames,
    pub diagnostics: mpsc::Receiver<String>,
    stop: Arc<AtomicBool>,
    pub finished: Arc<AtomicBool>,
    video: Option<Child>,
    audio: Option<Child>,
    sink: Option<Child>,
}
fn preview_size(canvas: [u32; 2], bounds: [u32; 2]) -> (usize, usize) {
    let w = canvas[0].max(2) as f64;
    let h = canvas[1].max(2) as f64;
    let factor = (bounds[0].clamp(2, 4096) as f64 / w)
        .min(bounds[1].clamp(2, 4096) as f64 / h)
        .min(1.);
    (
        ((w * factor / 2.).floor() as usize * 2).max(2),
        ((h * factor / 2.).floor() as usize * 2).max(2),
    )
}
fn frame_rate(fps: f64) -> Result<f64> {
    if !fps.is_finite() || fps < 1. {
        bail!("Invalid preview frame rate");
    }
    Ok(fps.min(120.))
}
fn timestamp(offset: f64, index: u64, fps: f64) -> f64 {
    offset + index as f64 / fps
}
fn pump(
    mut output: impl Read,
    width: usize,
    height: usize,
    fps: f64,
    offset: f64,
    flag: Arc<AtomicBool>,
    ended: Arc<AtomicBool>,
    frames: LatestFrames,
    repaint: egui::Context,
) {
    let mut index = 0u64;
    let mut started = None;
    loop {
        let mut rgba = vec![0; width * height * 4];
        if flag.load(Ordering::Relaxed) || output.read_exact(&mut rgba).is_err() {
            break;
        }
        let clock = started.get_or_insert_with(Instant::now);
        let deadline = Duration::from_secs_f64(index as f64 / fps);
        while clock.elapsed() < deadline && !flag.load(Ordering::Relaxed) {
            thread::sleep((deadline - clock.elapsed().min(deadline)).min(Duration::from_millis(1)));
        }
        if flag.load(Ordering::Relaxed) {
            break;
        }
        frames.publish(Frame {
            rgba,
            width,
            height,
            time: timestamp(offset, index, fps),
        });
        repaint.request_repaint();
        index += 1;
    }
    ended.store(true, Ordering::Release);
    repaint.request_repaint();
}
impl Playback {
    pub fn start_timeline(
        root: &Path,
        project: &Path,
        model: &serde_json::Value,
        project_snapshot: &serde_json::Value,
        offset: f64,
        canvas: [u32; 2],
        bounds: [u32; 2],
        fps: f64,
        repaint: egui::Context,
    ) -> Result<Self> {
        let fps = frame_rate(fps)?;
        let (width, height) = preview_size(canvas, bounds);
        let python = std::env::var("LAYER_PYTHON").unwrap_or_else(|_| "python3".into());
        let python_path = std::env::join_paths(std::iter::once(root.to_path_buf()).chain(
            std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default()),
        ))?;
        let mut child = Command::new(python)
            .args(["-m", "katman.preview_stream"])
            .current_dir(root)
            .env("PYTHONPATH", python_path)
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("Start timeline playback")?;
        let mut input = child.stdin.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (diagnostic_tx, diagnostics) = mpsc::sync_channel(8);
        let notifier = repaint.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                let Ok(line) = line else { break };
                let line = line.trim();
                if !line.is_empty() {
                    let _ = diagnostic_tx.try_send(line.chars().take(512).collect());
                    notifier.request_repaint();
                }
            }
        });
        let payload = serde_json::json!({"path":project,"model":model,"project_snapshot":project_snapshot,"position":offset,"size":[width,height],"fps":fps,"proxy":false});
        let frames = LatestFrames::default();
        let pending = frames.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let finished = Arc::new(AtomicBool::new(false));
        let ended = finished.clone();
        let output = child.stdout.take().unwrap();
        thread::spawn(move || {
            if serde_json::to_writer(&mut input, &payload).is_err()
                || input.write_all(b"\n").is_err()
            {
                ended.store(true, Ordering::Release);
                repaint.request_repaint();
                return;
            }
            drop(input);
            pump(
                output, width, height, fps, offset, flag, ended, pending, repaint,
            );
        });
        Ok(Self {
            frames,
            diagnostics,
            stop,
            finished,
            video: Some(child),
            audio: None,
            sink: None,
        })
    }
    /// Compatibility reader for an already rendered preview. Keep its source
    /// rate rather than converting every file to a fixed preview cadence.
    pub fn start(path: &str, offset: f64, canvas: [u32; 2]) -> Result<Self> {
        let info = Command::new("ffprobe")
            .args([
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate",
                "-of",
                "json",
                path,
            ])
            .output()
            .context("Read preview frame rate")?;
        let data: serde_json::Value = serde_json::from_slice(&info.stdout)?;
        let rate = data["streams"][0]["avg_frame_rate"]
            .as_str()
            .unwrap_or("30/1");
        let mut pair = rate.split('/');
        let a = pair.next().unwrap_or("30").parse::<f64>().unwrap_or(30.);
        let b = pair.next().unwrap_or("1").parse::<f64>().unwrap_or(1.);
        Self::start_with_options(
            path,
            offset,
            canvas,
            canvas,
            frame_rate(a / b)?,
            egui::Context::default(),
        )
    }
    pub fn start_with_options(
        path: &str,
        offset: f64,
        canvas: [u32; 2],
        bounds: [u32; 2],
        fps: f64,
        repaint: egui::Context,
    ) -> Result<Self> {
        let fps = frame_rate(fps)?;
        let (width, height) = preview_size(canvas, bounds);
        let scale = format!("scale={width}:{height}");
        let mut video = Command::new("ffmpeg")
            .args([
                "-v",
                "error",
                "-ss",
                &offset.to_string(),
                "-i",
                path,
                "-an",
                "-vf",
                &scale,
                "-r",
                &fps.to_string(),
                "-pix_fmt",
                "rgba",
                "-f",
                "rawvideo",
                "pipe:1",
            ])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("Start preview decoder")?;
        let output = video.stdout.take().unwrap();
        let frames = LatestFrames::default();
        let pending = frames.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let finished = Arc::new(AtomicBool::new(false));
        let ended = finished.clone();
        thread::spawn(move || {
            pump(
                output, width, height, fps, offset, flag, ended, pending, repaint,
            )
        });
        let mut audio = None;
        let mut sink = None;
        if let Ok(mut decoder) = Command::new("ffmpeg")
            .args([
                "-v",
                "error",
                "-ss",
                &offset.to_string(),
                "-i",
                path,
                "-vn",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "pipe:1",
            ])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        {
            let pipe = decoder.stdout.take().unwrap();
            match Command::new("paplay")
                .args([
                    "--raw",
                    "--format=s16le",
                    "--rate=48000",
                    "--channels=2",
                    "--client-name=Layer",
                    "--stream-name=Preview",
                ])
                .process_group(0)
                .stdin(Stdio::from(pipe))
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                Ok(player) => {
                    sink = Some(player);
                    audio = Some(decoder);
                }
                Err(_) => {
                    let _ = decoder.kill();
                    thread::spawn(move || {
                        let _ = decoder.wait();
                    });
                }
            }
        }
        let (_, diagnostics) = mpsc::channel();
        Ok(Self {
            frames,
            diagnostics,
            stop,
            finished,
            video: Some(video),
            audio,
            sink,
        })
    }
}
impl Drop for Playback {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let mut children: Vec<Child> = [self.video.take(), self.audio.take(), self.sink.take()]
            .into_iter()
            .flatten()
            .collect();
        for child in &mut children {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.kill();
        }
        thread::spawn(move || {
            for mut child in children {
                let _ = child.wait();
            }
        });
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn requested_bounds_preserve_aspect_and_allow_sharp_output() {
        assert_eq!(preview_size([3840, 1080], [1920, 1080]), (1920, 540));
        assert_eq!(preview_size([1080, 1920], [1920, 1080]), (606, 1080));
        assert_eq!(preview_size([1920, 1080], [3840, 2160]), (1920, 1080));
        assert_eq!(preview_size([3840, 2160], [3840, 2160]), (3840, 2160));
    }
    #[test]
    fn source_rates_have_exact_timestamps_and_cap_without_interpolation() {
        for fps in [60., 120.] {
            assert!((timestamp(2., 120, fps) - (2. + 120. / fps)).abs() < 1e-10);
        }
        assert_eq!(frame_rate(240.).unwrap(), 120.);
        assert!(frame_rate(f64::NAN).is_err());
    }
    #[test]
    fn stalled_consumer_gets_only_latest_frame() {
        let slot = LatestFrames::default();
        for index in 0..120 {
            slot.publish(Frame {
                rgba: vec![index as u8; 16],
                width: 2,
                height: 2,
                time: index as f64 / 120.,
            });
        }
        let frames: Vec<_> = slot.try_iter().collect();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].rgba[0], 119);
        assert_eq!(slot.try_iter().count(), 0);
    }
    #[test]
    fn source_rate_pump_publishes_timestamped_latest_frame() {
        for fps in [60., 120.] {
            let bytes: Vec<u8> = (0..4).flat_map(|value| vec![value; 16]).collect();
            let frames = LatestFrames::default();
            let ended = Arc::new(AtomicBool::new(false));
            pump(
                std::io::Cursor::new(bytes),
                2,
                2,
                fps,
                1.5,
                Arc::new(AtomicBool::new(false)),
                ended.clone(),
                frames.clone(),
                egui::Context::default(),
            );
            let frame = frames.try_iter().next().unwrap();
            assert_eq!(frame.rgba, vec![3; 16]);
            assert!((frame.time - (1.5 + 3. / fps)).abs() < 1e-10);
            assert!(ended.load(Ordering::Acquire));
            assert_eq!(frames.try_iter().count(), 0);
        }
    }
    #[test]
    fn cancellation_stops_frame_delivery() {
        let frames = LatestFrames::default();
        let stop = Arc::new(AtomicBool::new(true));
        let ended = Arc::new(AtomicBool::new(false));
        pump(
            std::io::Cursor::new(vec![0; 32]),
            2,
            2,
            120.,
            0.,
            stop,
            ended.clone(),
            frames.clone(),
            egui::Context::default(),
        );
        assert!(ended.load(Ordering::Acquire));
        assert_eq!(frames.try_iter().count(), 0);
    }
}
