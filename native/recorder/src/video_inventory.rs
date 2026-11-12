// SPDX-License-Identifier: GPL-3.0-only
//! Only display inventory runs here. Capture controls never wait for an EGL probe.
use crate::platform::Platform;
use serde_json::Value;
use std::{path::Path, sync::mpsc::{self, Receiver, SyncSender}, thread};
pub struct Inventory {
    requests: SyncSender<Value>,
    results: Receiver<Result<Value, String>>,
    pending: bool,
}
impl Inventory {
    pub fn new(root: &Path) -> Self {
        let (tx, requests) = mpsc::sync_channel(1);
        let (results, rx) = mpsc::channel();
        let root = root.to_path_buf();
        thread::spawn(move || {
            let mut platform = match Platform::module(&root, "katman.display_inventory") {
                Ok(value) => value,
                Err(error) => { let _ = results.send(Err(format!("{error:#}"))); return; }
            };
            for request in requests {
                let reply=platform.call_with("video-plan",request).map_err(|e|format!("{e:#}"));
                if results.send(reply).is_err() { break; }
            }
        });
        Self { requests: tx, results: rx, pending: false }
    }
    pub fn pending(&self) -> bool { self.pending }
    pub fn request(&mut self, settings: Value) -> Result<(), String> {
        if self.pending { return Ok(()); }
        self.requests.try_send(settings).map_err(|e|e.to_string())?;
        self.pending=true; Ok(())
    }
    pub fn poll(&mut self) -> Option<Result<Value,String>> {
        match self.results.try_recv() {
            Ok(result)=>{self.pending=false;Some(result)},
            Err(mpsc::TryRecvError::Empty)=>None,
            Err(mpsc::TryRecvError::Disconnected)=>{self.pending=false;Some(Err("Display inventory worker stopped".into()))},
        }
    }
}
