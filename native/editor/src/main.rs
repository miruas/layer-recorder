// SPDX-License-Identifier: GPL-3.0-only
mod app;
mod inspector;
mod model;
mod jobs;
mod ipc;
mod playback;
mod fast_preview;
mod gpu_effects;
mod preview_settings;
mod preview_toolbar;
mod overlay_cache;
mod voiceover;
mod autosave;
fn main(){if let Err(error)=app::run(){eprintln!("{error:#}");std::process::exit(1);}}
