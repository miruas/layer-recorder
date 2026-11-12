// SPDX-License-Identifier: GPL-3.0-only
use anyhow::{bail,Context,Result};
use serde_json::{Value,json};
use std::{fs,io::{BufRead,BufReader,Write},os::{fd::AsRawFd,unix::{fs::{PermissionsExt,OpenOptionsExt},net::{UnixListener,UnixStream}}},path::{Path,PathBuf},process::{Command,Stdio},sync::mpsc,time::Duration,thread};
pub fn runtime()->PathBuf{PathBuf::from(std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_|format!("/tmp/katman-{}",unsafe{libc::getuid()}))).join("katman")}
pub fn cache()->PathBuf{PathBuf::from(std::env::var("XDG_CACHE_HOME").unwrap_or_else(|_|format!("{}/.cache",std::env::var("HOME").unwrap_or_default()))).join("katman")}
pub fn settings_path()->PathBuf{std::env::var("LAYER_CONFIG").map(PathBuf::from).unwrap_or_else(|_|PathBuf::from(std::env::var("XDG_CONFIG_HOME").unwrap_or_else(|_|format!("{}/.config",std::env::var("HOME").unwrap_or_default()))).join("katman/settings.json"))}
pub fn settings()->Result<Value>{
 let path=settings_path();let mut cfg=if path.is_file(){serde_json::from_slice(&fs::read(path)?)?}else{json!({})};
 if cfg["library"].is_null(){cfg["library"]=json!("~/Videos/Layer");}
 for(k,v)in[("buffer",300),("replay_short",60),("replay_long",300)]{if cfg[k].is_null(){cfg[k]=json!(v);}}Ok(cfg)
}
pub fn resolved_settings(root:&Path)->Result<Value>{
 let python=std::env::var("LAYER_PYTHON").unwrap_or_else(|_|"python3".into());
 let out=Command::new(python).args(["-c","import json; from katman.common import settings; print(json.dumps(settings()))"]).env("PYTHONPATH",root).current_dir(root).output()?;
 if !out.status.success(){bail!("{}",String::from_utf8_lossy(&out.stderr).trim());}Ok(serde_json::from_slice(&out.stdout)?)
}
pub fn save_library(library:&str)->Result<()> {
 let target=expand(library);fs::create_dir_all(target)?;
 let file=settings_path();let mut raw:Value=if file.is_file(){serde_json::from_slice(&fs::read(&file)?)?}else{json!({})};
 if !raw.is_object(){bail!("Layer settings must be an object.");}raw["library"]=json!(library);
 let parent=file.parent().context("Settings folder")?;fs::create_dir_all(parent)?;
 let mut tmp=tempfile::NamedTempFile::new_in(parent)?;serde_json::to_writer_pretty(&mut tmp,&raw)?;tmp.flush()?;tmp.as_file().sync_all()?;tmp.persist(file)?;Ok(())
}
pub fn expand(path:&str)->PathBuf{if path=="~"||path.starts_with("~/"){PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(path.trim_start_matches('~').trim_start_matches('/'))}else{PathBuf::from(path)}}
pub fn request(message:Value)->Result<Value>{let mut s=UnixStream::connect(runtime().join("control.sock"))?;s.set_read_timeout(Some(Duration::from_secs(180)))?;serde_json::to_writer(&mut s,&message)?;s.write_all(b"\n")?;s.flush()?;let mut line=String::new();BufReader::new(s).read_line(&mut line)?;let reply:Value=serde_json::from_str(&line)?;if reply["ok"]!=true{bail!("{}",reply["error"].as_str().unwrap_or("Capture operation failed"));}Ok(reply["data"].clone())}
pub fn ensure_core(root:&Path)->Result<()>{if request(json!({"action":"status"})).is_ok(){return Ok(());}let binary=root.join("bin/layer-recorderd");if !binary.is_file(){bail!("The capture core is missing. Install the complete Layer package or build the recorder component.");}let cache=cache();fs::create_dir_all(&cache)?;let log=fs::OpenOptions::new().create(true).append(true).open(cache.join("service.log"))?;Command::new(binary).args(["--root",&root.to_string_lossy()]).env("LAYER_PYTHON",std::env::var("LAYER_PYTHON").unwrap_or_else(|_|"python3".into())).stdin(Stdio::null()).stdout(log.try_clone()?).stderr(log).spawn()?;for _ in 0..100{thread::sleep(Duration::from_millis(50));if request(json!({"action":"status"})).is_ok(){return Ok(());}}bail!("The capture core did not start. Check service.log.")}
/// Canonical GSR control actions exposed to the editor. Keeping this list here
/// prevents the UI from inventing capture commands or falling back to Layer-only
/// capture while GSR is unavailable.
const GSR_ACTIONS:&[&str]=&[
 "toggle-record","start-record","stop-record",
 "toggle-replay","start-replay","stop-replay",
 "replay-save","replay-save-1-min","replay-save-10-min",
 "toggle-pause","toggle-stream","start-stream","stop-stream",
 "toggle-record-region","toggle-record-window",
 "take-screenshot","take-screenshot-region","take-screenshot-window",
 "show-recording-settings","toggle-show","status",
];
/// Run one validated GSR action through the Python CLI bridge.
///
/// This never uses a shell, never reads GSR's configuration, and never turns a
/// successful socket write into a claim that media was saved or a stream started.
pub fn gsr_action(root:&Path,action:&str)->Result<Value>{
 if !GSR_ACTIONS.contains(&action){bail!("Unsupported GPU Screen Recorder action.");}
 let python=std::env::var("LAYER_PYTHON").unwrap_or_else(|_|"python3".into());
 let out=Command::new(python)
  .args(["-m","katman","gsr",action])
  .env("PYTHONPATH",root)
  .current_dir(root)
  .stdin(Stdio::null())
  .output()
  .context("Run GPU Screen Recorder control")?;
 if !out.status.success(){
  let message=String::from_utf8_lossy(&out.stderr);
  let message=message.trim();
  bail!("{}",if message.is_empty(){"GPU Screen Recorder did not accept the command. Start its overlay and try again."}else{message});
 }
 let stdout=String::from_utf8_lossy(&out.stdout);
 let stdout=stdout.trim();
 if stdout.is_empty(){return Ok(json!({"ok":true}));}
 Ok(serde_json::from_str(stdout).unwrap_or_else(|_|json!({"message":stdout})))
}
pub fn open_gsr(root:&Path)->Result<()>{gsr_action(root,"show-recording-settings").map(|_|())}
pub struct Instance{_lock:fs::File,pub incoming:mpsc::Receiver<Option<PathBuf>>}
pub fn instance(open:Option<PathBuf>)->Result<Option<Instance>>{
 let dir=runtime();fs::create_dir_all(&dir)?;fs::set_permissions(&dir,fs::Permissions::from_mode(0o700))?;let socket=dir.join("studio.sock");let lock=fs::OpenOptions::new().create(true).read(true).write(true).truncate(false).custom_flags(libc::O_NOFOLLOW).open(dir.join("studio.lock"))?;
 if unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0{let mut s=UnixStream::connect(&socket).context("Existing Layer window is starting; try again shortly")?;serde_json::to_writer(&mut s,&json!({"open":open}))?;s.write_all(b"\n")?;return Ok(None);}
 if socket.exists(){fs::remove_file(&socket)?;}let listener=UnixListener::bind(&socket)?;fs::set_permissions(&socket,fs::Permissions::from_mode(0o600))?;let(tx,rx)=mpsc::channel();thread::spawn(move||{for s in listener.incoming(){let Ok(s)=s else{break;};let _=s.set_read_timeout(Some(Duration::from_secs(2)));let mut line=String::new();if BufReader::new(s).read_line(&mut line).is_ok(){if let Ok(v)=serde_json::from_str::<Value>(&line){let _=tx.send(v["open"].as_str().map(PathBuf::from));}}}});Ok(Some(Instance{_lock:lock,incoming:rx}))
}
#[cfg(test)]
mod tests{
 use super::*;
 #[test]
 fn gsr_action_rejects_unknown_commands_before_spawning(){
  let error=gsr_action(Path::new("."),"definitely-not-gsr").unwrap_err().to_string();
  assert!(error.contains("Unsupported GPU Screen Recorder action"));
 }
 #[test]
 fn gsr_action_allowlist_covers_editor_controls(){
  for action in ["toggle-record","toggle-replay","replay-save-1-min","replay-save-10-min","toggle-pause","toggle-stream","toggle-record-region","take-screenshot","take-screenshot-region","take-screenshot-window","show-recording-settings","toggle-show","status","start-record","stop-record","start-replay","stop-replay","start-stream","stop-stream"]{
   assert!(GSR_ACTIONS.contains(&action),"{action}");
  }
 }
}
