// SPDX-License-Identifier: GPL-3.0-only
use serde_json::Value;
use std::{io::{BufRead,BufReader,Write},path::{Path,PathBuf},process::{Command,Stdio},os::unix::process::CommandExt,sync::{Arc,atomic::{AtomicU32,Ordering},mpsc},thread};
#[derive(Debug)]pub enum Event{Progress(u64,f64),Done(u64,String,Result<Value,String>)}
pub struct Jobs{tx:mpsc::Sender<Event>,pub rx:mpsc::Receiver<Event>,next:u64,pub running:Vec<(u64,String,Arc<AtomicU32>)>}
impl Jobs{
 pub fn new()->Self{let(tx,rx)=mpsc::channel();Self{tx,rx,next:0,running:vec![]}}
 pub fn start(&mut self,root:&Path,operation:&str,mut request:Value)->u64{
  self.next+=1;let id=self.next;request["operation"]=Value::String(operation.into());let op=operation.to_string();let root:PathBuf=root.into();let tx=self.tx.clone();let pid=Arc::new(AtomicU32::new(0));let child_pid=pid.clone();self.running.push((id,op.clone(),pid));
  thread::spawn(move||{
   let result=(||->anyhow::Result<Value>{let python=std::env::var("LAYER_PYTHON").unwrap_or_else(|_|"python3".into());let mut child=Command::new(python).args(["-m","katman.editor_worker"]).env("PYTHONPATH",std::env::join_paths(std::iter::once(root.clone()).chain(std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default()))).unwrap_or_else(|_|root.as_os_str().to_os_string())).current_dir(&root).process_group(0).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn()?;child_pid.store(child.id(),Ordering::SeqCst);let mut input=child.stdin.take().unwrap();serde_json::to_writer(&mut input,&request)?;input.write_all(b"\n")?;drop(input);let mut final_result=None;
    for line in BufReader::new(child.stdout.take().unwrap()).lines(){let line=line?;let Ok(value)=serde_json::from_str::<Value>(&line)else{continue;};if value["event"]=="progress"{let _=tx.send(Event::Progress(id,value["value"].as_f64().unwrap_or(0.)));}else{final_result=Some(value);}}
    let status=child.wait()?;child_pid.store(0,Ordering::SeqCst);let value=final_result.ok_or_else(||anyhow::anyhow!("Media worker stopped without a result ({status})."))?;if value["ok"]!=true{anyhow::bail!("{}",value["error"].as_str().unwrap_or("Media operation failed"));}Ok(value["data"].clone())})();child_pid.store(0,Ordering::SeqCst);let _=tx.send(Event::Done(id,op,result.map_err(|e|format!("{e:#}"))));
  });id
 }
 pub fn finished(&mut self,id:u64){self.running.retain(|(n,_,_)|*n!=id);}
 pub fn cancel(&self,id:u64){if let Some((_,_,pid))=self.running.iter().find(|(n,_,_)|*n==id){let pid=pid.load(Ordering::SeqCst);if pid>0{unsafe{libc::kill(-(pid as i32),libc::SIGTERM);}}}}
 pub fn busy(&self)->bool{self.running.iter().any(|(_,op,_)|op!="frame"&&op!="waveforms")}
}
impl Drop for Jobs{fn drop(&mut self){for(id,_,_)in &self.running{self.cancel(*id);}}}
