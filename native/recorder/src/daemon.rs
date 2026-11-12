// SPDX-License-Identifier: GPL-3.0-only
//! Wire-compatible local JSON IPC. Cached status never waits for a replay save.
use crate::{capture::Recorder, util::now};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
            net::{UnixListener, UnixStream},
        },
    },
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc::{self, SyncSender},
    },
    thread,
    time::Duration,
};
struct Job {
    message: Value,
    reply: mpsc::SyncSender<Value>,
}
fn private(path: &Path) -> Result<()> {
    match fs::create_dir(path) {
        Ok(()) => (),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(e.into()),
    };
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::getuid() } {
        bail!("Control directory must be owned by this user and must not be a symlink.");
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}
pub fn serve(root: &Path, runtime: PathBuf, cache: PathBuf) -> Result<()> {
    let base = runtime.parent().context("Runtime directory")?;
    private(base)?;
    private(&runtime)?;
    fs::create_dir_all(&cache)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(runtime.join("daemon.lock"))?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Ok(());
    }
    let socket = runtime.join("control.sock");
    if socket.exists() {
        fs::remove_file(&socket)?;
    }
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    let stop = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(libc::SIGINT, stop.clone())?;
    signal_hook::flag::register(libc::SIGTERM, stop.clone())?;
    let mut recorder = Recorder::new(root, cache)?;
    let cached = Arc::new(Mutex::new(recorder.status()));
    let (tx, rx) = mpsc::sync_channel::<Job>(32);
    let actor_stop = stop.clone();
    let actor_cache = cached.clone();
    let actor = thread::spawn(move || {
        if recorder.autostart() {
            if let Err(e) = recorder.toggle_replay() {
                eprintln!("Replay startup: {e:#}");
                recorder.error(format!("Replay startup: {e:#}"));
            }
        }
        *actor_cache.lock().unwrap() = recorder.status();
        let mut last_tick = 0.;
        while !actor_stop.load(Ordering::Relaxed) {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(job) => {
                    let shutdown = job.message["action"] == "shutdown";
                    let result = handle(&mut recorder, &job.message);
                    let success = result.is_ok();
                    let response = match result {
                        Ok(value) => json!({"ok":true,"data":value}),
                        Err(e) => json!({"ok":false,"error":format!("{e:#}")}),
                    };
                    *actor_cache.lock().unwrap() = recorder.status();
                    let _ = job.reply.send(response);
                    if shutdown && success {
                        actor_stop.store(true, Ordering::Relaxed);
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => (),
                Err(_) => break,
            }
            if now() - last_tick >= 0.5 {
                recorder.tick();
                *actor_cache.lock().unwrap() = recorder.status();
                last_tick = now();
            }
        }
        if let Err(e) = recorder.shutdown() {
            eprintln!("Shutdown retained recoverable capture: {e:#}");
        }
    });
    let clients = Arc::new(AtomicUsize::new(0));
    while !stop.load(Ordering::Relaxed) {
        match listener.accept() {
            Ok((stream, _)) => {
                if clients.load(Ordering::Relaxed) >= 64 {
                    drop(stream);
                    continue;
                }
                clients.fetch_add(1, Ordering::Relaxed);
                let cache = cached.clone();
                let tx = tx.clone();
                let count = clients.clone();
                thread::spawn(move || {
                    let _ = connection(stream, cache, tx);
                    count.fetch_sub(1, Ordering::Relaxed);
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                let mut fd = libc::pollfd {
                    fd: listener.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                unsafe {
                    libc::poll(&mut fd, 1, 100);
                }
            }
            Err(e) => {
                stop.store(true, Ordering::Relaxed);
                let _ = actor.join();
                let _ = fs::remove_file(&socket);
                return Err(e.into());
            }
        }
    }
    drop(listener);
    drop(tx);
    let _ = actor.join();
    fs::remove_file(socket)?;
    Ok(())
}
fn connection(
    mut stream: UnixStream,
    cached: Arc<Mutex<Value>>,
    tx: SyncSender<Job>,
) -> Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut size = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut _ as *mut _,
            &mut size,
        )
    } != 0
        || credentials.uid != unsafe { libc::getuid() }
    {
        bail!("Unauthorized control peer.");
    }
    let response = (|| -> Result<Value> {
        let mut bytes = Vec::new();
        BufReader::new((&mut stream).take(1_048_577)).read_until(b'\n', &mut bytes)?;
        if bytes.len() > 1_048_576 || bytes.last() != Some(&b'\n') {
            bail!("Control request must be one JSON line no larger than 1 MiB.");
        }
        let message: Value = serde_json::from_slice(&bytes)?;
        if message["action"] == "status" {
            return Ok(json!({"ok":true,"data":cached.lock().unwrap().clone()}));
        }
        let (reply, rx) = mpsc::sync_channel(1);
        tx.try_send(Job { message, reply })
            .map_err(|_| anyhow::anyhow!("Capture control queue is busy. Retry shortly."))?;
        Ok(rx
            .recv_timeout(Duration::from_secs(175))
            .context("Capture operation timed out")?)
    })();
    let response = response.unwrap_or_else(|e| json!({"ok":false,"error":format!("{e:#}")}));
    serde_json::to_writer(&mut stream, &response)?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    Ok(())
}
fn handle(r: &mut Recorder, m: &Value) -> Result<Value> {
    match m["action"].as_str() {
        Some("gsr-action") => r.gsr_action(m["command"].as_str().context("Missing GSR action")?,m["seconds"].as_u64()),
        Some("set-capture-state") => r.set_state(
            m["kind"].as_str().context("Invalid capture kind")?,
            m["enabled"]
                .as_bool()
                .context("Capture state must be boolean")?,
        ),
        Some("set-paused") => r.set_paused(m["enabled"].as_bool().context("Pause state must be boolean")?),
        Some("toggle-pause") => r.toggle_pause(),
        Some("reload-settings") => r.reload(),
        Some("toggle-record") => r.toggle_record(),
        Some("toggle-replay") => r.toggle_replay(),
        Some("save-replay") => r.save_replay(
            m["seconds"]
                .as_u64()
                .context("Replay duration must be an integer")?,
        ),
        Some("shutdown") => {
            r.shutdown()?;
            Ok(json!("stopped"))
        }
        _ => bail!("Unknown action."),
    }
}
