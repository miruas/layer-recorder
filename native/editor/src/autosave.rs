// SPDX-License-Identifier: GPL-3.0-only
//! Coalesced project persistence. Only this worker touches the filesystem;
//! submitting edits and requesting a flush never wait for a disk operation.
use anyhow::{Context, Result, bail};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};
use tempfile::NamedTempFile;

const COALESCE: Duration = Duration::from_millis(100);

#[derive(Debug)]
pub enum Event {
    Saved {
        path: PathBuf,
        revision: u64,
        result: std::result::Result<(), String>,
    },
    Flushed(u64),
}

struct Snapshot {
    path: PathBuf,
    fallback_project: Value,
    model: Value,
    revision: u64,
}

#[derive(Default)]
struct State {
    latest: BTreeMap<PathBuf, Snapshot>,
    wake_queued: bool,
}

enum Command {
    Wake,
    Flush(u64),
    Barrier(Vec<PathBuf>, mpsc::Sender<std::result::Result<(), String>>),
}

pub struct Autosave {
    sender: mpsc::Sender<Command>,
    pub results: mpsc::Receiver<Event>,
    shared: Arc<Mutex<State>>,
}

impl Autosave {
    pub fn new() -> Self {
        let (sender, requests) = mpsc::channel();
        let (events, results) = mpsc::channel();
        let shared = Arc::new(Mutex::new(State::default()));
        let worker_shared = shared.clone();
        // Dropping the handle detaches the worker. GUI shutdown waits for an
        // asynchronous Flush acknowledgement instead of joining on the UI thread.
        thread::spawn(move || worker(requests, events, worker_shared));
        Self {
            sender,
            results,
            shared,
        }
    }

    pub fn submit(&self, path: PathBuf, fallback_project: Value, model: Value, revision: u64) {
        // Keep one latest model per document even if a filesystem read blocks
        // the worker. The command queue contains only lightweight notifications.
        let mut state = self
            .shared
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        state.latest.insert(
            path.clone(),
            Snapshot {
                path,
                fallback_project,
                model,
                revision,
            },
        );
        if !state.wake_queued {
            state.wake_queued = true;
            let _ = self.sender.send(Command::Wake);
        }
    }

    pub fn barrier(&self, path: PathBuf) -> mpsc::Receiver<std::result::Result<(), String>> {
        self.barrier_all(vec![path])
    }

    pub fn barrier_all(
        &self,
        paths: Vec<PathBuf>,
    ) -> mpsc::Receiver<std::result::Result<(), String>> {
        let (reply, result) = mpsc::channel();
        let _ = self.sender.send(Command::Barrier(paths, reply));
        result
    }

    pub fn flush(&self, token: u64) {
        let _ = self.sender.send(Command::Flush(token));
    }
}

fn normalized_path(mut path: PathBuf) -> PathBuf {
    if path.is_dir() {
        path = path.join("project.katman");
    }
    fs::canonicalize(&path).unwrap_or_else(|_| {
        if let (Some(parent), Some(name)) = (path.parent(), path.file_name()) {
            if let Ok(parent) = fs::canonicalize(parent) {
                return parent.join(name);
            }
        }
        path
    })
}

fn store(snapshot: &Snapshot) -> Result<()> {
    let mut project = match fs::read(&snapshot.path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes).with_context(|| {
            format!(
                "Could not read valid project JSON from {}",
                snapshot.path.display()
            )
        })?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            snapshot.fallback_project.clone()
        }
        Err(error) => {
            return Err(error)
                .with_context(|| format!("Could not read {}", snapshot.path.display()));
        }
    };
    if !project.is_object() {
        bail!(
            "The project root must be a JSON object: {}",
            snapshot.path.display()
        );
    }
    // Imports and other workers may have updated sources or unknown metadata
    // after the document opened. Merge only this editor's current studio state.
    // Source imports are returned to the editor in fallback_project. They do
    // not rewrite the disk project themselves. Append only unseen source IDs;
    // existing disk source objects and unknown root metadata remain authoritative.
    if let Some(imported) = snapshot
        .fallback_project
        .get("sources")
        .and_then(Value::as_array)
    {
        if !imported.is_empty() {
            if project.get("sources").is_none() {
                project["sources"] = Value::Array(vec![]);
            }
            let Some(current) = project.get_mut("sources").and_then(Value::as_array_mut) else {
                bail!(
                    "The project source list is invalid: {}",
                    snapshot.path.display()
                );
            };
            for source in imported {
                let Some(id) = source.get("id").and_then(Value::as_str) else {
                    continue;
                };
                if !current
                    .iter()
                    .any(|existing| existing.get("id").and_then(Value::as_str) == Some(id))
                {
                    current.push(source.clone());
                }
            }
        }
    }
    project["studio"] = snapshot.model.clone();
    let parent = snapshot
        .path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent).with_context(|| format!("Could not create {}", parent.display()))?;
    let mut temporary = NamedTempFile::new_in(parent).with_context(|| {
        format!(
            "Could not create a temporary project in {}",
            parent.display()
        )
    })?;
    serde_json::to_writer_pretty(&mut temporary, &project)
        .context("Could not write the project file")?;
    temporary.write_all(b"\n")?;
    temporary
        .flush()
        .context("Could not flush the project file")?;
    temporary
        .as_file()
        .sync_all()
        .context("Could not sync the project file")?;
    temporary.persist(&snapshot.path).map_err(|error| {
        anyhow::anyhow!(
            "Could not replace {}: {}",
            snapshot.path.display(),
            error.error
        )
    })?;
    Ok(())
}

fn write_pending(
    pending: &mut BTreeMap<PathBuf, Snapshot>,
    failed: &mut BTreeMap<PathBuf, Snapshot>,
    events: &mpsc::Sender<Event>,
) {
    for (path, snapshot) in std::mem::take(pending) {
        let result = store(&snapshot).map_err(|error| format!("{error:#}"));
        let revision = snapshot.revision;
        if result.is_err() {
            failed.insert(path.clone(), snapshot);
        } else {
            failed.remove(&path);
        }
        let _ = events.send(Event::Saved {
            path,
            revision,
            result,
        });
    }
}

fn flush_all(
    pending: &mut BTreeMap<PathBuf, Snapshot>,
    failed: &mut BTreeMap<PathBuf, Snapshot>,
    events: &mpsc::Sender<Event>,
) {
    for (path, snapshot) in std::mem::take(failed) {
        pending.entry(path).or_insert(snapshot);
    }
    write_pending(pending, failed, events);
}

fn collect_latest(
    shared: &Arc<Mutex<State>>,
    pending: &mut BTreeMap<PathBuf, Snapshot>,
    failed: &mut BTreeMap<PathBuf, Snapshot>,
    clear_wake: bool,
) {
    let latest = {
        let mut state = shared.lock().unwrap_or_else(|error| error.into_inner());
        if clear_wake {
            state.wake_queued = false;
        }
        std::mem::take(&mut state.latest)
    };
    // No filesystem calls or model serialization occur while holding the
    // shared mutex. Only a dequeued Wake owns/reset the notification flag.
    for (_, mut snapshot) in latest {
        snapshot.path = normalized_path(snapshot.path);
        failed.remove(&snapshot.path);
        pending.insert(snapshot.path.clone(), snapshot);
    }
}

fn worker(
    requests: mpsc::Receiver<Command>,
    events: mpsc::Sender<Event>,
    shared: Arc<Mutex<State>>,
) {
    let mut pending = BTreeMap::new();
    let mut failed = BTreeMap::new();
    let mut deadline: Option<Instant> = None;
    loop {
        // Check before receiving: recv_timeout(0) can still receive a queued
        // message. This deadline must not be starved by a continuous drag.
        if deadline.is_some_and(|time| Instant::now() >= time) {
            collect_latest(&shared, &mut pending, &mut failed, false);
            write_pending(&mut pending, &mut failed, &events);
            deadline = None;
        }
        let message = if let Some(time) = deadline {
            requests.recv_timeout(time.saturating_duration_since(Instant::now()))
        } else {
            requests
                .recv()
                .map_err(|_| mpsc::RecvTimeoutError::Disconnected)
        };
        match message {
            Ok(Command::Wake) => {
                collect_latest(&shared, &mut pending, &mut failed, true);
                if !pending.is_empty() {
                    deadline.get_or_insert_with(|| Instant::now() + COALESCE);
                }
            }
            Ok(Command::Barrier(paths, reply)) => {
                collect_latest(&shared, &mut pending, &mut failed, false);
                write_pending(&mut pending, &mut failed, &events);
                deadline = None;
                let failed_path = paths
                    .into_iter()
                    .map(normalized_path)
                    .find(|path| failed.contains_key(path));
                let result = if let Some(path) = failed_path {
                    Err(format!(
                        "Edits could not be saved for {}. Retry saving before continuing.",
                        path.display()
                    ))
                } else {
                    Ok(())
                };
                let _ = reply.send(result);
            }
            Ok(Command::Flush(token)) => {
                collect_latest(&shared, &mut pending, &mut failed, false);
                flush_all(&mut pending, &mut failed, &events);
                deadline = None;
                let _ = events.send(Event::Flushed(token));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                collect_latest(&shared, &mut pending, &mut failed, false);
                write_pending(&mut pending, &mut failed, &events);
                deadline = None;
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                collect_latest(&shared, &mut pending, &mut failed, false);
                flush_all(&mut pending, &mut failed, &events);
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{ffi::CString, os::unix::fs::OpenOptionsExt};

    fn read(path: &Path) -> Value {
        serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
    }
    fn until_flush(
        autosave: &Autosave,
        token: u64,
    ) -> Vec<(PathBuf, u64, std::result::Result<(), String>)> {
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut saved = vec![];
        loop {
            match autosave
                .results
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap()
            {
                Event::Saved {
                    path,
                    revision,
                    result,
                } => saved.push((path, revision, result)),
                Event::Flushed(value) if value == token => return saved,
                Event::Flushed(_) => (),
            }
        }
    }

    #[test]
    fn coalesces_latest_model_and_preserves_current_sources_and_unknown_metadata() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        let stale = json!({"sources":[{"id":"old"}],"opaque":{"value":"stale"},"studio":{"x":0}});
        fs::write(&path, serde_json::to_vec(&stale).unwrap()).unwrap();
        let autosave = Autosave::new();
        for revision in 1..=32 {
            autosave.submit(path.clone(), stale.clone(), json!({"x":revision}), revision);
        }
        let current = json!({"sources":[{"id":"old"},{"id":"imported","custom":true}],"opaque":{"value":"new","future":[1,2,3]},"studio":{"x":0}});
        fs::write(&path, serde_json::to_vec(&current).unwrap()).unwrap();
        autosave.flush(5);
        let saved = until_flush(&autosave, 5);
        assert_eq!(saved.len(), 1);
        assert_eq!(saved[0].1, 32);
        assert!(saved[0].2.is_ok(), "{:?}", saved[0].2);
        let mut expected = current;
        expected["studio"] = json!({"x":32});
        assert_eq!(read(&path), expected);
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn imported_sources_are_added_without_overwriting_existing_source_metadata() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        fs::write(&path, br#"{"sources":[{"id":"old","file":"original.mkv","new_metadata":true}],"opaque":"keep"}"#).unwrap();
        let autosave = Autosave::new();
        let fallback = json!({"sources":[{"id":"old","file":"stale.mkv"},{"id":"import","file":"imported.mka","role":"voiceover"}],"opaque":"stale"});
        let model = json!({"tracks":[{"clips":[{"source_id":"import"}]}]});
        autosave.submit(path.clone(), fallback, model.clone(), 9);
        autosave.flush(4);
        assert!(
            until_flush(&autosave, 4)
                .iter()
                .all(|(_, _, result)| result.is_ok())
        );
        assert_eq!(
            read(&path),
            json!({"sources":[{"id":"old","file":"original.mkv","new_metadata":true},{"id":"import","file":"imported.mka","role":"voiceover"}],"opaque":"keep","studio":model})
        );
    }

    #[test]
    fn invalid_json_is_preserved_and_failed_snapshot_retries_after_switching_documents() {
        let directory = tempfile::tempdir().unwrap();
        let broken = directory.path().join("broken.katman");
        let other = directory.path().join("other.katman");
        fs::write(&broken, b"{incomplete").unwrap();
        let autosave = Autosave::new();
        autosave.submit(
            broken.clone(),
            json!({"sources":[]}),
            json!({"edit":"retained"}),
            7,
        );
        autosave.flush(1);
        let failed = until_flush(&autosave, 1);
        assert_eq!(failed.len(), 1);
        assert!(failed[0].2.is_err());
        assert_eq!(fs::read(&broken).unwrap(), b"{incomplete");
        // An unrelated document must not replace the recoverable snapshot.
        autosave.submit(
            other.clone(),
            json!({"sources":[],"opaque":9}),
            json!({"x":3}),
            2,
        );
        fs::write(
            &broken,
            br#"{"sources":[{"id":"recovered"}],"opaque":"keep"}"#,
        )
        .unwrap();
        autosave.flush(2);
        let saved = until_flush(&autosave, 2);
        assert_eq!(saved.len(), 2);
        assert!(saved.iter().all(|(_, _, result)| result.is_ok()));
        assert_eq!(
            read(&broken),
            json!({"sources":[{"id":"recovered"}],"opaque":"keep","studio":{"edit":"retained"}})
        );
        assert_eq!(
            read(&other),
            json!({"sources":[],"opaque":9,"studio":{"x":3}})
        );
    }

    #[test]
    fn barrier_persists_imported_sources_and_does_not_retry_failed_snapshots() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        fs::write(&path, br#"{"sources":[],"opaque":"keep"}"#).unwrap();
        let autosave = Autosave::new();
        autosave.submit(
            path.clone(),
            json!({"sources":[{"id":"import","file":"new.mka"}]}),
            json!({"tracks":[{"source_id":"import"}]}),
            1,
        );
        // Directory paths are normalized by the worker, not by the caller.
        autosave
            .barrier(directory.path().to_owned())
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .unwrap();
        assert_eq!(read(&path)["sources"][0]["id"], "import");
        assert_eq!(read(&path)["opaque"], "keep");
        fs::write(&path, b"broken JSON").unwrap();
        autosave.submit(path.clone(), json!({"sources":[]}), json!({"x":2}), 2);
        assert!(
            autosave
                .barrier(path.clone())
                .recv_timeout(Duration::from_secs(3))
                .unwrap()
                .is_err()
        );
        let other = directory.path().join("other.katman");
        fs::write(&other, br#"{"sources":[]}"#).unwrap();
        autosave
            .barrier(other.clone())
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .unwrap();
        assert!(
            autosave
                .barrier_all(vec![other, path.clone()])
                .recv_timeout(Duration::from_secs(3))
                .unwrap()
                .is_err()
        );
        // Repairing the file alone must not turn an ordinary barrier into an
        // implicit retry: the explicit flush/retry policy remains authoritative.
        fs::write(&path, br#"{"sources":[],"opaque":"repaired"}"#).unwrap();
        assert!(
            autosave
                .barrier(path.clone())
                .recv_timeout(Duration::from_secs(3))
                .unwrap()
                .is_err()
        );
        assert!(read(&path).get("studio").is_none());
        autosave.flush(9);
        assert!(until_flush(&autosave, 9).last().unwrap().2.is_ok());
        autosave
            .barrier(path.clone())
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .unwrap();
        assert_eq!(read(&path)["studio"], json!({"x":2}));
    }

    #[test]
    fn blocked_fifo_stress_retains_only_one_latest_model_per_path() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        let name = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let (sender, requests) = mpsc::channel();
        let (events, results) = mpsc::channel();
        let shared = Arc::new(Mutex::new(State::default()));
        let worker_shared = shared.clone();
        let handle = thread::spawn(move || worker(requests, events, worker_shared));
        let autosave = Autosave {
            sender,
            results,
            shared,
        };
        autosave.submit(path.clone(), json!({"sources":[]}), json!({"x":0}), 0);
        autosave.flush(20);
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut writer = loop {
            match fs::OpenOptions::new()
                .write(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&path)
            {
                Ok(writer) => break writer,
                Err(error) if error.raw_os_error() == Some(libc::ENXIO) => {
                    assert!(
                        Instant::now() < deadline,
                        "Worker did not begin its FIFO read"
                    );
                    thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("{error}"),
            }
        };
        let started = Instant::now();
        for revision in 1..=1000 {
            autosave.submit(
                path.clone(),
                json!({"sources":[]}),
                json!({"x":revision,"points":vec![revision;128]}),
                revision,
            );
        }
        let elapsed = started.elapsed();
        let (count, revision, wake_queued) = {
            let state = autosave.shared.lock().unwrap();
            (
                state.latest.len(),
                state.latest.get(&path).map(|snapshot| snapshot.revision),
                state.wake_queued,
            )
        };
        autosave.flush(21);
        // Always unblock the real FIFO before asserting, then join to prove
        // this stress case leaves no blocked persistence thread behind.
        writer
            .write_all(br#"{"sources":[],"opaque":"keep"}"#)
            .unwrap();
        drop(writer);
        let saved = until_flush(&autosave, 21);
        let project = read(&path);
        drop(autosave);
        handle.join().unwrap();
        assert_eq!(count, 1);
        assert_eq!(revision, Some(1000));
        assert!(wake_queued);
        assert!(elapsed < Duration::from_secs(1));
        assert!(
            saved.iter().all(|(_, _, result)| result.is_ok()),
            "{saved:?}"
        );
        assert_eq!(project["studio"]["x"], 1000);
        assert_eq!(project["studio"]["points"], json!(vec![1000; 128]));
        assert_eq!(project["opaque"], "keep");
    }

    #[test]
    fn blocked_fifo_read_does_not_block_submit_and_flush_persists_latest_state() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        let name = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        // Retain this test worker's handle to prove it exits after unblocking.
        // Production intentionally does not join from Drop.
        let (sender, requests) = mpsc::channel();
        let (events, results) = mpsc::channel();
        let shared = Arc::new(Mutex::new(State::default()));
        let worker_shared = shared.clone();
        let handle = thread::spawn(move || worker(requests, events, worker_shared));
        let autosave = Autosave {
            sender,
            results,
            shared,
        };
        autosave.submit(path.clone(), json!({}), json!({"x":1}), 1);
        autosave.flush(10);
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut writer = loop {
            match fs::OpenOptions::new()
                .write(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&path)
            {
                Ok(writer) => break writer,
                Err(error) if error.raw_os_error() == Some(libc::ENXIO) => {
                    assert!(
                        Instant::now() < deadline,
                        "Worker did not begin its FIFO read"
                    );
                    thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("{error}"),
            }
        };
        let started = Instant::now();
        autosave.submit(path.clone(), json!({"stale":true}), json!({"x":2}), 2);
        autosave.flush(11);
        let submit_elapsed = started.elapsed();
        // Unblock before assertions, ensuring this test leaves no blocked worker.
        writer
            .write_all(br#"{"sources":[{"id":"original"}],"opaque":"keep"}"#)
            .unwrap();
        drop(writer);
        let saved = until_flush(&autosave, 11);
        let project = read(&path);
        drop(autosave);
        handle.join().unwrap();
        assert!(submit_elapsed < Duration::from_secs(1));
        assert!(
            saved.iter().all(|(_, _, result)| result.is_ok()),
            "{saved:?}"
        );
        assert_eq!(
            project,
            json!({"sources":[{"id":"original"}],"opaque":"keep","studio":{"x":2}})
        );
    }
}
