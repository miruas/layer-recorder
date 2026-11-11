// SPDX-License-Identifier: GPL-3.0-only
use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU32, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

#[derive(Debug)]
pub enum Event {
    Progress(u64, f64),
    Done(u64, String, Result<Value, String>),
}

pub struct Control {
    pid: AtomicU32,
    cancelled: AtomicBool,
}
impl Control {
    fn kill_group(&self, signal: i32) {
        let pid = self.pid.load(Ordering::SeqCst);
        if pid > 0 {
            unsafe {
                libc::kill(-(pid as i32), signal);
            }
        }
    }
    fn check(&self) -> anyhow::Result<()> {
        if self.cancelled.load(Ordering::SeqCst) {
            anyhow::bail!("Media operation cancelled.");
        }
        Ok(())
    }
}

// All waits, including cleanup after an I/O error, run on the job thread.
struct Process {
    child: Child,
    control: Arc<Control>,
}
impl Drop for Process {
    fn drop(&mut self) {
        if !matches!(self.child.try_wait(), Ok(Some(_))) {
            self.control.kill_group(libc::SIGKILL);
            let _ = self.child.wait();
        }
        self.control.pid.store(0, Ordering::SeqCst);
    }
}

pub struct Jobs {
    tx: mpsc::Sender<Event>,
    pub rx: mpsc::Receiver<Event>,
    next: u64,
    pub running: Vec<(u64, String, Arc<Control>)>,
}

impl Jobs {
    pub fn new() -> Self {
        let (tx, rx) = mpsc::channel();
        Self {
            tx,
            rx,
            next: 0,
            running: vec![],
        }
    }
    pub fn start(&mut self, root: &Path, operation: &str, request: Value) -> u64 {
        self.start_inner(root, operation, request, None)
    }
    pub fn start_after(
        &mut self,
        root: &Path,
        operation: &str,
        request: Value,
        gate: mpsc::Receiver<Result<(), String>>,
    ) -> u64 {
        self.start_inner(root, operation, request, Some(gate))
    }
    fn start_inner(
        &mut self,
        root: &Path,
        operation: &str,
        mut request: Value,
        gate: Option<mpsc::Receiver<Result<(), String>>>,
    ) -> u64 {
        self.next += 1;
        let id = self.next;
        request["operation"] = Value::String(operation.into());
        let op = operation.to_owned();
        let root: PathBuf = root.into();
        let tx = self.tx.clone();
        let control = Arc::new(Control {
            pid: AtomicU32::new(0),
            cancelled: AtomicBool::new(false),
        });
        self.running.push((id, op.clone(), control.clone()));
        thread::spawn(move || {
            let result = (|| -> anyhow::Result<Value> {
                if let Some(gate) = gate {
                    loop {
                        control.check()?;
                        match gate.recv_timeout(Duration::from_millis(50)) {
                            Ok(result) => {
                                result.map_err(anyhow::Error::msg)?;
                                break;
                            }
                            Err(mpsc::RecvTimeoutError::Timeout) => continue,
                            Err(mpsc::RecvTimeoutError::Disconnected) => anyhow::bail!(
                                "Saving stopped before the media operation could start."
                            ),
                        }
                    }
                }
                control.check()?;
                let python = std::env::var("LAYER_PYTHON").unwrap_or_else(|_| "python3".into());
                let python_path = std::env::join_paths(std::iter::once(root.clone()).chain(
                    std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default()),
                ))
                .unwrap_or_else(|_| root.as_os_str().to_os_string());
                let child = Command::new(python)
                    .args(["-m", "katman.editor_worker"])
                    .env("PYTHONPATH", python_path)
                    .current_dir(&root)
                    .process_group(0)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::inherit())
                    .spawn()?;
                control.pid.store(child.id(), Ordering::SeqCst);
                let mut process = Process {
                    child,
                    control: control.clone(),
                };
                // A cancel can arrive after the pre-spawn check and before pid
                // publication. Check again so that race cannot launch live work.
                control.check()?;
                let mut input = process.child.stdin.take().unwrap();
                serde_json::to_writer(&mut input, &request)?;
                input.write_all(b"\n")?;
                drop(input);
                let mut final_result = None;
                for line in BufReader::new(process.child.stdout.take().unwrap()).lines() {
                    let line = line?;
                    let Ok(value) = serde_json::from_str::<Value>(&line) else {
                        continue;
                    };
                    if value["event"] == "progress" {
                        let _ = tx.send(Event::Progress(id, value["value"].as_f64().unwrap_or(0.)));
                    } else {
                        final_result = Some(value);
                    }
                }
                let status = process.child.wait()?;
                control.check()?;
                let value = final_result.ok_or_else(|| {
                    anyhow::anyhow!("Media worker stopped without a result ({status}).")
                })?;
                if value["ok"] != true {
                    anyhow::bail!(
                        "{}",
                        value["error"].as_str().unwrap_or("Media operation failed")
                    );
                }
                Ok(value["data"].clone())
            })();
            control.pid.store(0, Ordering::SeqCst);
            let result = if control.cancelled.load(Ordering::SeqCst) {
                Err(anyhow::anyhow!("Media operation cancelled."))
            } else {
                result
            };
            let _ = tx.send(Event::Done(
                id,
                op,
                result.map_err(|error| format!("{error:#}")),
            ));
        });
        id
    }
    pub fn finished(&mut self, id: u64) {
        self.running.retain(|(number, _, _)| *number != id);
    }
    pub fn cancel(&self, id: u64) {
        if let Some((_, _, control)) = self.running.iter().find(|(number, _, _)| *number == id) {
            control.cancelled.store(true, Ordering::SeqCst);
            control.kill_group(libc::SIGTERM);
        }
    }
    pub fn busy(&self) -> bool {
        self.running
            .iter()
            .any(|(_, operation, _)| operation != "frame" && operation != "waveforms")
    }
}
impl Drop for Jobs {
    fn drop(&mut self) {
        for (id, _, _) in &self.running {
            self.cancel(*id);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{fs, time::Instant};

    #[test]
    fn cancelling_a_blocked_persistence_gate_finishes_without_spawning_python() {
        let directory = tempfile::tempdir().unwrap();
        let package = directory.path().join("katman");
        fs::create_dir(&package).unwrap();
        fs::write(package.join("__init__.py"), "").unwrap();
        let marker = directory.path().join("spawned");
        fs::write(
            package.join("editor_worker.py"),
            "from pathlib import Path\nPath('spawned').write_text('unexpected worker')\n",
        )
        .unwrap();
        let (reply, gate) = mpsc::channel();
        let mut jobs = Jobs::new();
        let id = jobs.start_after(directory.path(), "export", json!({}), gate);
        let started = Instant::now();
        jobs.cancel(id);
        match jobs.rx.recv_timeout(Duration::from_secs(2)).unwrap() {
            Event::Done(done, operation, result) => {
                assert_eq!(done, id);
                assert_eq!(operation, "export");
                assert!(result.unwrap_err().contains("cancelled"));
            }
            event => panic!("Unexpected event: {event:?}"),
        }
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(jobs.running[0].2.pid.load(Ordering::SeqCst), 0);
        assert!(!marker.exists());
        // The gate was never opened, yet its receiver has already gone away.
        assert!(reply.send(Ok(())).is_err());
        jobs.finished(id);
        assert!(!jobs.busy());
    }
}
