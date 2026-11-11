// SPDX-License-Identifier: GPL-3.0-only
mod app;
mod inspector;
mod model;
mod jobs;
mod ipc;
mod playback;
mod voiceover;
mod autosave;
fn main(){if let Err(error)=app::run(){eprintln!("{error:#}");std::process::exit(1);}}
