// SPDX-License-Identifier: GPL-3.0-only
//! Microphone capture uses GSR on both PipeWire and PulseAudio. Every process
//! and final media probe lives off the GUI thread, including cancellation.
use anyhow::{bail, Context, Result};
use std::io::{Read, Seek};
use std::{fs,os::unix::process::CommandExt,path::{Path,PathBuf},process::{Command,Stdio},sync::mpsc,thread,time::{Duration,Instant}};
pub struct Capture { pub _directory: tempfile::TempDir, pub file: PathBuf, pub duration: f64 }
enum Control { Finish, Cancel }
pub struct Voiceover {
    controls:mpsc::Sender<Control>, results:mpsc::Receiver<Result<Capture,String>>,
    pub ready:Option<Capture>, pub error:Option<String>, pub stopping:bool, started:Instant,
}
fn bounded_output(command:&mut Command, timeout:Duration)->Result<std::process::Output> {
    let mut file=tempfile::tempfile()?;
    command.stdin(Stdio::null()).stdout(file.try_clone()?).stderr(Stdio::null()).process_group(0);
    let mut child=command.spawn()?;
    let deadline=Instant::now()+timeout;
    let status=loop {
        if let Some(status)=child.try_wait()?{break status;}
        if Instant::now()>deadline {
            unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL);}
            let _=child.wait();bail!("The sound service did not respond in time.");
        }
        thread::sleep(Duration::from_millis(20));
    };
    file.rewind()?;let mut stdout=vec![];file.take(1024*1024).read_to_end(&mut stdout)?;
    Ok(std::process::Output{status,stdout,stderr:vec![]})
}
pub fn devices(program:&Path)->Result<Vec<(String,String)>> {
    let output=bounded_output(Command::new(program).arg("--list-audio-devices"),Duration::from_secs(8))?;
    if !output.status.success(){bail!("The microphone list is unavailable. Check your desktop sound service.");}
    Ok(String::from_utf8_lossy(&output.stdout).lines().filter_map(|line|line.split_once('|')).filter(|(name,_)|*name!="default_output"&&!name.ends_with(".monitor")).map(|(name,label)|(name.into(),label.into())).collect())
}
impl Voiceover {
    pub fn start(program:PathBuf,device:String,cache:PathBuf)->Self {
        let(tx,rx)=mpsc::channel();let(result_tx,result_rx)=mpsc::channel();
        thread::spawn(move||{
            let result=(||->Result<Capture>{
                fs::create_dir_all(&cache)?;
                let directory=tempfile::Builder::new().prefix("voiceover-").tempdir_in(cache)?;
                let file=directory.path().join("Voiceover.mka");
                let logfile=directory.path().join("capture.log");
                let log=fs::File::create(&logfile)?;
                let mut command=Command::new(program);
                command.args(["-a",&device,"-ac","opus","-ab","256","-c","matroska","-o",&file.to_string_lossy(),"-v","no"])
                    .stdin(Stdio::null()).stdout(Stdio::null()).stderr(log).process_group(0);
                unsafe {command.pre_exec(||{if libc::prctl(libc::PR_SET_PDEATHSIG,libc::SIGINT)!=0{return Err(std::io::Error::last_os_error());}Ok(())});}
                let mut child=command.spawn().context("Start microphone capture")?;
                let mut requested=false;let mut cancelled=false;let mut deadline=None;
                loop {
                    match rx.try_recv(){
                        Ok(control)=>{cancelled=matches!(control,Control::Cancel);requested=true;unsafe{libc::kill(child.id() as i32,libc::SIGINT);}deadline=Some(Instant::now()+Duration::from_secs(5));},
                        Err(mpsc::TryRecvError::Disconnected)=>{cancelled=true;requested=true;let _=child.kill();},
                        Err(mpsc::TryRecvError::Empty)=>(),
                    }
                    if let Some(status)=child.try_wait()? {
                        if cancelled{bail!("Microphone recording cancelled.");}
                        if !requested||!status.success(){let details=fs::read_to_string(&logfile).unwrap_or_default();bail!("Microphone capture stopped. {}",details.chars().rev().take(1800).collect::<String>().chars().rev().collect::<String>());}
                        break;
                    }
                    if deadline.is_some_and(|t|Instant::now()>t){let _=child.kill();let _=child.wait();bail!("The microphone did not finish in time. No clip was added.");}
                    thread::sleep(Duration::from_millis(20));
                }
                let output=bounded_output(Command::new("ffprobe").args(["-v","error","-show_entries","stream=codec_type:format=duration","-of","json",&file.to_string_lossy()]),Duration::from_secs(8))?;
                if !output.status.success(){bail!("The microphone recording could not be read.");}
                let info:serde_json::Value=serde_json::from_slice(&output.stdout)?;
                let duration=info["format"]["duration"].as_str().and_then(|s|s.parse::<f64>().ok()).unwrap_or(0.);
                if duration<0.1||!info["streams"].as_array().is_some_and(|streams|streams.iter().any(|s|s["codec_type"]=="audio")){bail!("The recording is too short. Record at least a moment of microphone audio.");}
                Ok(Capture{_directory:directory,file,duration})
            })().map_err(|error|format!("{error:#}"));
            let _=result_tx.send(result);
        });
        Self{controls:tx,results:result_rx,ready:None,error:None,stopping:false,started:Instant::now()}
    }
    pub fn finish(&mut self){if !self.stopping{self.stopping=true;let _=self.controls.send(Control::Finish);}}
    pub fn elapsed(&self)->f64{self.started.elapsed().as_secs_f64()}
    pub fn poll(&mut self){for result in self.results.try_iter(){match result{Ok(capture)=>self.ready=Some(capture),Err(error)=>self.error=Some(error)}}}
}
impl Drop for Voiceover{fn drop(&mut self){let _=self.controls.send(Control::Cancel);}}
#[cfg(test)]
mod tests {
 use super::*;
 use std::os::unix::fs::PermissionsExt;
 #[test]
 fn finish_keeps_valid_audio_until_import_and_then_cleans_cache(){
  let root=tempfile::tempdir().unwrap();let fixture=root.path().join("tone.mka");
  let result=Command::new("ffmpeg").args(["-v","error","-f","lavfi","-i","sine=duration=0.3:sample_rate=48000","-c:a","libopus",&fixture.to_string_lossy()]).status().unwrap();assert!(result.success());
  let program=root.path().join("capture");
  fs::write(&program,format!("#!/bin/sh\nwhile [ \"$1\" != \"-o\" ]; do shift; done\nshift\ncp '{}' \"$1\"\ntrap 'exit 0' INT TERM\nwhile :; do sleep 0.02; done\n",fixture.display())).unwrap();
  fs::set_permissions(&program,fs::Permissions::from_mode(0o700)).unwrap();
  let cache=root.path().join("cache");let mut session=Voiceover::start(program,"default_input".into(),cache.clone());
  let deadline=Instant::now()+Duration::from_secs(5);
  while !cache.exists()||fs::read_dir(&cache).unwrap().next().is_none(){assert!(Instant::now()<deadline);thread::sleep(Duration::from_millis(10));}
  thread::sleep(Duration::from_millis(100));session.finish();
  while session.ready.is_none()&&session.error.is_none(){assert!(Instant::now()<deadline);session.poll();thread::sleep(Duration::from_millis(10));}
  assert!(session.error.is_none(),"{:?}",session.error);
  let captured=session.ready.as_ref().unwrap().file.clone();assert!(captured.exists());assert!(session.ready.as_ref().unwrap().duration>=0.3);
  drop(session);assert!(!captured.exists());assert!(fixture.exists());
 }
}
