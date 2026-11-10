// SPDX-License-Identifier: GPL-3.0-only
use crate::util::{id, log_tail, now};
use anyhow::Result;
use serde_json::Value;
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
#[derive(Clone, Debug)]
pub struct Segment {
    pub path: PathBuf,
    pub start: f64,
    pub end: f64,
}
pub struct Track {
    pub id: String,
    pub key: String,
    pub meta: Value,
    pub path: PathBuf,
    pub offset: f64,
    pub origin: f64,
    pub producer: Child,
    pub closed: bool,
    pub reported: bool,
    pub rows: Vec<Segment>,
    csv_offset: u64,
    pending: String,
    pub activity: Vec<(f64, Option<f64>)>,
    pub presence: Vec<(f64, Option<f64>)>,
    pub selector: String,
}
impl Track {
    pub fn spawn(
        root: &Path,
        origin: f64,
        key: String,
        meta: Value,
        args: &[String],
        selector: String,
    ) -> Result<Self> {
        let id = id()[..12].to_string();
        let path = root.join(&id);
        fs::create_dir(&path)?;
        let argv: Vec<String> = args
            .iter()
            .map(|a| {
                a.replace("{output}", &path.join("%08d.mkv").to_string_lossy())
                    .replace("{list}", &path.join("segments.csv").to_string_lossy())
            })
            .collect();
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path.join("capture.log"))?;
        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(log))
            .process_group(0);
        // Stop orphaned encoders if the daemon crashes; no privilege changes.
        unsafe {
            cmd.pre_exec(|| {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGINT) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let offset = now() - origin;
        let producer = cmd.spawn()?;
        Ok(Self {
            id,
            key,
            meta,
            path,
            offset,
            origin,
            producer,
            closed: false,
            reported: false,
            rows: Vec::new(),
            csv_offset: 0,
            pending: String::new(),
            activity: Vec::new(),
            presence: Vec::new(),
            selector,
        })
    }
    pub fn alive(&mut self) -> bool {
        !self.closed && self.producer.try_wait().ok().flatten().is_none()
    }
    pub fn signal(&mut self) {
        if self.alive() {
            unsafe {
                libc::kill(self.producer.id() as i32, libc::SIGINT);
            }
        }
    }
    pub fn stop(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.signal();
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if self.producer.try_wait()?.is_some() {
                break;
            }
            if Instant::now() > deadline {
                self.producer.kill()?;
                self.producer.wait()?;
                break;
            }
            thread::sleep(Duration::from_millis(20));
        }
        self.closed = true;
        self.segments(false)?;
        Ok(())
    }
    pub fn segments(&mut self, include_active: bool) -> Result<Vec<Segment>> {
        if let Ok(s) = fs::read_to_string(self.path.join("%08d.mkv.ts")) {
            if let Some(t) = s
                .lines()
                .nth(1)
                .and_then(|l| l.split_whitespace().next())
                .and_then(|x| x.parse::<f64>().ok())
            {
                self.offset = t / 1e6 - self.origin;
            }
        }
        if let Ok(mut f) = File::open(self.path.join("segments.csv")) {
            f.seek(SeekFrom::Start(self.csv_offset))?;
            let mut appended = String::new();
            f.read_to_string(&mut appended)?;
            self.csv_offset = f.stream_position()?;
            self.pending.push_str(&appended);
            if let Some(i) = self.pending.rfind('\n') {
                let complete = self.pending[..=i].to_string();
                self.pending = self.pending[i + 1..].to_string();
                for record in csv::ReaderBuilder::new()
                    .has_headers(false)
                    .from_reader(complete.as_bytes())
                    .records()
                {
                    let r = record?;
                    if r.len() != 3 {
                        continue;
                    }
                    let Some(name) = Path::new(&r[0]).file_name() else {
                        continue;
                    };
                    if let (Ok(start), Ok(end)) = (r[1].parse::<f64>(), r[2].parse::<f64>()) {
                        if start.is_finite() && end.is_finite() && end >= start {
                            self.rows.push(Segment {
                                path: self.path.join(name),
                                start,
                                end,
                            });
                        }
                    }
                }
            }
        }
        let mut rows = self.rows.clone();
        if include_active {
            let mut extras: Vec<PathBuf> = fs::read_dir(&self.path)?
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| {
                    p.extension().is_some_and(|e| e == "mkv")
                        && !rows.iter().any(|r| r.path.file_name() == p.file_name())
                })
                .collect();
            extras.sort();
            for path in extras {
                let start = rows.last().map(|r| r.end).unwrap_or_else(|| {
                    path.file_stem()
                        .and_then(|x| x.to_str())
                        .and_then(|x| x.parse::<f64>().ok())
                        .unwrap_or(0.)
                        * 2.
                });
                rows.push(Segment {
                    path,
                    start,
                    end: f64::INFINITY,
                });
            }
        }
        Ok(rows)
    }
    pub fn prune(
        &mut self,
        cutoff: f64,
        archive_before: Option<f64>,
        archive: &Path,
    ) -> Result<()> {
        self.segments(false)?;
        let mut kept = Vec::new();
        for mut row in self.rows.drain(..) {
            if self.offset + row.end < cutoff {
                if row.path.exists() {
                    fs::remove_file(row.path)?;
                }
                continue;
            }
            if archive_before.is_some_and(|time| self.offset + row.end < time)
                && row.path.parent() == Some(self.path.as_path())
            {
                let parent = archive.join(&self.id);
                fs::create_dir_all(&parent)?;
                let target = parent.join(row.path.file_name().unwrap());
                fs::copy(&row.path, &target)?;
                fs::remove_file(&row.path)?;
                row.path = target;
            }
            kept.push(row);
        }
        self.rows = kept;
        for intervals in [&mut self.activity, &mut self.presence] {
            intervals.retain(|(_, end)| end.is_none_or(|end| end >= cutoff));
        }
        Ok(())
    }
    pub fn active_in(&self, start: f64, end: f64) -> bool {
        self.meta["type"] != "audio"
            || self.meta["kind"] == "microphone"
            || self
                .activity
                .iter()
                .chain(self.presence.iter())
                .any(|(a, b)| start.max(*a) < end.min(b.unwrap_or(end)) - 0.001)
    }
    pub fn error(&self) -> String {
        format!(
            "{}: the capture source stopped. {}",
            self.meta["name"].as_str().unwrap_or("Source"),
            log_tail(&self.path.join("capture.log"))
        )
    }
    pub fn cache_bytes(&self) -> u64 {
        fs::read_dir(&self.path)
            .ok()
            .into_iter()
            .flatten()
            .filter_map(|e| e.ok())
            .filter(|e| e.path().extension().is_some_and(|x| x == "mkv"))
            .filter_map(|e| e.metadata().ok())
            .map(|m| m.len())
            .sum()
    }
}
impl Drop for Track {
    fn drop(&mut self) {
        if !self.closed {
            let _ = self.stop();
        }
    }
}
pub fn interval(intervals: &mut Vec<(f64, Option<f64>)>, enabled: bool, now: f64) {
    let open = intervals.last().is_some_and(|(_, end)| end.is_none());
    if enabled && !open {
        intervals.push((now, None));
    } else if !enabled && open {
        intervals.last_mut().unwrap().1 = Some(now);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn presence_closes_and_reopens() {
        let mut a = vec![];
        interval(&mut a, true, 1.);
        interval(&mut a, true, 2.);
        interval(&mut a, false, 3.);
        interval(&mut a, true, 4.);
        assert_eq!(a, vec![(1., Some(3.)), (4., None)]);
    }
}

#[cfg(test)]
mod storage_tests {
    use super::*;
    fn track(root: &Path) -> Track {
        Track::spawn(
            root,
            now(),
            "qa".into(),
            serde_json::json!({"type":"video","name":"Synthetic QA"}),
            &["sleep".into(), "60".into()],
            String::new(),
        )
        .unwrap()
    }
    #[test]
    fn spool_moves_only_closed_segments_and_pruning_removes_expired_media() {
        let root = tempfile::tempdir().unwrap();
        let archive = tempfile::tempdir().unwrap();
        let mut t = track(root.path());
        t.offset = 0.;
        let mut csv = String::new();
        for (i, (start, end)) in [(0, 2), (302, 304), (308, 310)].iter().enumerate() {
            let p = t.path.join(format!("{i:08}.mkv"));
            fs::write(&p, vec![i as u8; 100]).unwrap();
            csv.push_str(&format!("{}, {}, {}\n", p.display(), start, end).replace(", ", ","));
        }
        fs::write(t.path.join("segments.csv"), csv).unwrap();
        t.prune(-3., Some(306.), archive.path()).unwrap();
        assert_eq!(t.rows.len(), 3);
        assert!(t.rows[0].path.starts_with(archive.path()));
        assert!(t.rows[1].path.starts_with(archive.path()));
        assert!(t.rows[2].path.starts_with(&t.path));
        assert_eq!(fs::read(&t.rows[1].path).unwrap(), vec![1; 100]);
        t.prune(306., None, archive.path()).unwrap();
        assert_eq!(t.rows.len(), 1);
        assert_eq!(t.rows[0].start, 308.);
        assert!(!archive.path().join(&t.id).join("00000000.mkv").exists());
    }
    #[test]
    fn incomplete_csv_is_not_duplicated_and_retired_app_expires() {
        use std::io::Write;
        let root = tempfile::tempdir().unwrap();
        let mut t = track(root.path());
        t.offset = 0.;
        let p = t.path.join("segments.csv");
        fs::write(&p, "00000000.mkv,0,2\n00000001.mkv,2,").unwrap();
        assert_eq!(t.segments(false).unwrap().len(), 1);
        OpenOptions::new()
            .append(true)
            .open(p)
            .unwrap()
            .write_all(b"4\n")
            .unwrap();
        assert_eq!(t.segments(false).unwrap().len(), 2);
        assert_eq!(t.segments(false).unwrap().len(), 2);
        t.meta = serde_json::json!({"type":"audio","kind":"application"});
        interval(&mut t.presence, true, 0.);
        interval(&mut t.presence, false, 10.);
        assert!(t.active_in(0., 20.));
        assert!(!t.active_in(60., 120.));
    }
}
