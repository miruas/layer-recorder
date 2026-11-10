// SPDX-License-Identifier: GPL-3.0-only
use anyhow::{Context, Result, bail};
use serde_json::Value;
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
pub fn now() -> f64 {
    let mut t: libc::timespec = unsafe { std::mem::zeroed() };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t) };
    t.tv_sec as f64 + t.tv_nsec as f64 / 1e9
}
pub fn id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
pub fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let parent = path.parent().context("JSON parent")?;
    fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.as_file().sync_all()?;
    file.persist(path)?;
    Ok(())
}
pub struct Output {
    pub ok: bool,
    pub stdout: Vec<u8>,
    pub stderr: String,
}
pub fn output(args: &[String], timeout: u64) -> Result<Output> {
    if args.is_empty() {
        bail!("Empty process command");
    }
    let mut p = Command::new(&args[0])
        .args(&args[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("Start {}", args[0]))?;
    let out = p.stdout.take().unwrap();
    let err = p.stderr.take().unwrap();
    let read = |r: Box<dyn Read + Send>| {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = r.take(8 * 1024 * 1024).read_to_end(&mut bytes);
            bytes
        })
    };
    let out = read(Box::new(out));
    let err = read(Box::new(err));
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let status = loop {
        if let Some(status) = p.try_wait()? {
            break status;
        }
        if Instant::now() > deadline {
            let _ = p.kill();
            let _ = p.wait();
            bail!("{} exceeded its {timeout}-second timeout", args[0]);
        }
        thread::sleep(Duration::from_millis(15));
    };
    Ok(Output {
        ok: status.success(),
        stdout: out.join().unwrap_or_default(),
        stderr: String::from_utf8_lossy(&err.join().unwrap_or_default()).into_owned(),
    })
}
pub fn run(args: &[String], timeout: u64) -> Result<Vec<u8>> {
    let o = output(args, timeout)?;
    if !o.ok {
        bail!("{} failed: {}", args[0], tail(&o.stderr, 1600));
    }
    Ok(o.stdout)
}
pub fn probe(path: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&run(
        &[
            "ffprobe".into(),
            "-v".into(),
            "error".into(),
            "-show_streams".into(),
            "-show_format".into(),
            "-of".into(),
            "json".into(),
            path.to_string_lossy().into_owned(),
        ],
        30,
    )?)?)
}
pub fn tail(s: &str, n: usize) -> &str {
    let start = s.len().saturating_sub(n);
    let start = (start..=s.len())
        .find(|&i| s.is_char_boundary(i))
        .unwrap_or(s.len());
    &s[start..]
}
pub fn log_tail(path: &Path) -> String {
    let mut s = String::new();
    if let Ok(mut f) = File::open(path) {
        use std::io::{Seek, SeekFrom};
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        let _ = f.seek(SeekFrom::Start(len.saturating_sub(1600)));
        let mut b = Vec::new();
        let _ = f.read_to_end(&mut b);
        s = String::from_utf8_lossy(&b).into_owned();
    }
    s
}
