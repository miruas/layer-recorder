// SPDX-License-Identifier: GPL-3.0-only
mod audio;
mod buffer_storage;
mod capture;
mod daemon;
mod platform;
mod track;
mod util;
use anyhow::{Result, bail};
use std::path::PathBuf;
fn main() {
    if let Err(e) = run() {
        eprintln!("{e:#}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).is_some_and(|s| s == "--version") {
        println!("layer-recorderd {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.get(1).is_some_and(|s| s == "ram-create") {
        println!("{}", serde_json::to_string(&buffer_storage::create()?)?);
        return Ok(());
    }
    let root = if args.len() == 3 && args[1] == "--root" {
        PathBuf::from(&args[2])
    } else {
        bail!("Usage: layer-recorderd --root APPLICATION_DIRECTORY | --version | ram-create");
    };
    let home = PathBuf::from(std::env::var("HOME")?);
    let runtime = PathBuf::from(
        std::env::var("XDG_RUNTIME_DIR")
            .unwrap_or_else(|_| format!("/tmp/katman-{}", unsafe { libc::getuid() })),
    )
    .join("katman");
    let cache = std::env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| home.join(".cache"))
        .join("katman");
    daemon::serve(&root, runtime, cache)
}
