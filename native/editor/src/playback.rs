// SPDX-License-Identifier: GPL-3.0-only
//! Decode a preview in the existing window. PulseAudio also covers PipeWire's
//! Pulse server; stopping decoders never waits for child processes on the GUI.
use anyhow::{Context, Result};
use std::{
    io::{Read, BufRead, BufReader},
    io::Write,
    os::unix::process::CommandExt,
    path::Path,
    process::{Child, Command, Stdio},
    sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc},
    thread,
    time::{Duration, Instant},
};
pub struct Frame { pub rgba: Vec<u8>, pub width: usize, pub height: usize, pub time: f64 }
pub struct Playback {
    pub frames: mpsc::Receiver<Frame>,
    pub diagnostics: mpsc::Receiver<String>,
    stop: Arc<AtomicBool>,
    pub finished: Arc<AtomicBool>,
    video: Option<Child>, audio: Option<Child>, sink: Option<Child>,
}
fn preview_size(canvas: [u32; 2]) -> (usize, usize) {
    let w = canvas[0].max(2) as f64;
    let h = canvas[1].max(2) as f64;
    let factor = (960. / w).min(640. / h).min(1.);
    (((w * factor / 2.).round() as usize * 2).max(2), ((h * factor / 2.).round() as usize * 2).max(2))
}
impl Playback {
    /// Stream the compositor directly. No intermediate MP4, disk write or
    /// full-timeline encode is needed before the first frame can be displayed.
    pub fn start_timeline(root: &Path, project: &Path, model: &serde_json::Value, project_snapshot: &serde_json::Value, offset: f64, canvas: [u32;2]) -> Result<Self> {
        let (width,height)=preview_size(canvas);
        let python=std::env::var("LAYER_PYTHON").unwrap_or_else(|_|"python3".into());
        let python_path=std::env::join_paths(std::iter::once(root.to_path_buf()).chain(std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default())))?;
        let mut child=Command::new(python).args(["-m","katman.preview_stream"]).current_dir(root).env("PYTHONPATH",python_path).process_group(0)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().context("Start timeline playback")?;
        let mut input=child.stdin.take().unwrap();
        let stderr=child.stderr.take().unwrap();
        let (diagnostic_tx,diagnostics)=mpsc::sync_channel(8);
        thread::spawn(move|| {
            for line in BufReader::new(stderr).lines() {
                let Ok(line)=line else {break};
                let line=line.trim();
                if !line.is_empty(){let _=diagnostic_tx.try_send(line.chars().take(512).collect());}
            }
        });
        let payload=serde_json::json!({"path":project,"model":model,"project_snapshot":project_snapshot,"position":offset,"size":[width,height],"fps":24});
        // Large timelines are serialized away from the GUI event loop.
        let (tx,rx)=mpsc::sync_channel(2);
        let stop=Arc::new(AtomicBool::new(false));let flag=stop.clone();
        let finished=Arc::new(AtomicBool::new(false));let ended=finished.clone();
        let mut output=child.stdout.take().unwrap();
        thread::spawn(move||{
            if serde_json::to_writer(&mut input,&payload).is_err()||input.write_all(b"\n").is_err(){ended.store(true,Ordering::Release);return;}
            drop(input);let mut index=0u64;let mut started=None;
            loop{
                let mut rgba=vec![0;width*height*4];
                if flag.load(Ordering::Relaxed)||output.read_exact(&mut rgba).is_err(){break;}
                let clock=started.get_or_insert_with(Instant::now);
                let deadline=Duration::from_secs_f64(index as f64/24.);
                while clock.elapsed()<deadline&&!flag.load(Ordering::Relaxed){thread::sleep(Duration::from_millis(2));}
                if flag.load(Ordering::Relaxed)||tx.send(Frame{rgba,width,height,time:offset+index as f64/24.}).is_err(){break;}
                index+=1;
            }
            ended.store(true,Ordering::Release);
        });
        Ok(Self{frames:rx,diagnostics,stop,finished,video:Some(child),audio:None,sink:None})
    }
    pub fn start(path: &str, offset: f64, canvas: [u32; 2]) -> Result<Self> {
        let (width, height) = preview_size(canvas);
        let scale = format!("scale={width}:{height}");
        let mut video = Command::new("ffmpeg").args(["-v","error","-ss",&offset.to_string(),"-i",path,"-an","-vf",&scale,"-r","24","-pix_fmt","rgba","-f","rawvideo","pipe:1"])
            .process_group(0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().context("Start preview decoder")?;
        let mut output = video.stdout.take().unwrap();
        let (tx, rx) = mpsc::sync_channel(2);
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let finished = Arc::new(AtomicBool::new(false));
        let ended = finished.clone();
        let started = Instant::now();
        thread::spawn(move || {
            let mut index = 0u64;
            loop {
                let mut rgba = vec![0; width * height * 4];
                if flag.load(Ordering::Relaxed) || output.read_exact(&mut rgba).is_err() { break; }
                let deadline = Duration::from_secs_f64(index as f64 / 24.);
                while started.elapsed() < deadline && !flag.load(Ordering::Relaxed) { thread::sleep(Duration::from_millis(2)); }
                if flag.load(Ordering::Relaxed) || tx.send(Frame { rgba, width, height, time: offset + index as f64 / 24. }).is_err() { break; }
                index += 1;
            }
            ended.store(true, Ordering::Release);
        });
        let mut audio = None;
        let mut sink = None;
        if let Ok(mut decoder) = Command::new("ffmpeg").args(["-v","error","-ss",&offset.to_string(),"-i",path,"-vn","-f","s16le","-ar","48000","-ac","2","pipe:1"])
            .process_group(0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn() {
            let pipe = decoder.stdout.take().unwrap();
            // paplay connects to either a native PulseAudio server or pipewire-pulse.
            match Command::new("paplay").args(["--raw","--format=s16le","--rate=48000","--channels=2","--client-name=Layer","--stream-name=Preview"])
                .process_group(0).stdin(Stdio::from(pipe)).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
                Ok(player) => { sink = Some(player); audio = Some(decoder); }
                Err(_) => { let _ = decoder.kill(); thread::spawn(move || { let _ = decoder.wait(); }); }
            }
        }
        let (_,diagnostics)=mpsc::channel();
        Ok(Self { frames: rx, diagnostics, stop, finished, video: Some(video), audio, sink })
    }
}
impl Drop for Playback {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let mut children: Vec<Child> = [self.video.take(), self.audio.take(), self.sink.take()].into_iter().flatten().collect();
        for child in &mut children { unsafe {libc::kill(-(child.id()as i32),libc::SIGKILL);} let _ = child.kill(); }
        thread::spawn(move || { for mut child in children { let _ = child.wait(); } });
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn aspect_ratio_is_retained_for_multi_display_and_portrait() {
        assert_eq!(preview_size([3840,1080]), (960,270));
        assert_eq!(preview_size([1080,1920]), (360,640));
        assert_eq!(preview_size([1920,1080]), (960,540));
        assert_eq!(preview_size([0,0]), (2,2));
    }
}
