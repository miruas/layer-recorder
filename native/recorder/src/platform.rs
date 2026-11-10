// SPDX-License-Identifier: GPL-3.0-only
//! Persistent configuration and optional compositor metadata bridge; no media crosses it.
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
};
pub struct Platform {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}
impl Platform {
    pub fn new(root: &Path) -> Result<Self> {
        let python = std::env::var("LAYER_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut child = Command::new(python)
            .args(["-m", "katman.native_platform"])
            .env("PYTHONPATH", root)
            .current_dir(root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .context("Start metadata bridge")?;
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Ok(Self {
            child,
            input,
            output,
        })
    }
    pub fn call(&mut self, op: &str) -> Result<Value> {
        serde_json::to_writer(&mut self.input, &json!({"op":op}))?;
        self.input.write_all(b"\n")?;
        self.input.flush()?;
        let mut line = String::new();
        if self.output.read_line(&mut line)? == 0 {
            bail!("The configuration bridge stopped.");
        }
        let v: Value = serde_json::from_str(&line)?;
        if v["ok"] != true {
            bail!("{}", v["error"].as_str().unwrap_or("Metadata bridge error"));
        }
        Ok(v["data"].clone())
    }
}
impl Drop for Platform {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
