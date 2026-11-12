// SPDX-License-Identifier: GPL-3.0-only
mod app;
mod advanced_capture;
mod inspector;
mod model;
mod jobs;
mod fast_export;
mod ipc;
mod playback;
mod fast_preview;
mod gpu_effects;
mod preview_settings;
mod media_rate;
mod preview_toolbar;
mod overlay_cache;
mod voiceover;
mod autosave;
fn main(){if let Some(result)=fast_export::cli(){if let Err(error)=result{eprintln!("{error:#}");std::process::exit(1);}return;}if let Err(error)=app::run(){eprintln!("{error:#}");std::process::exit(1);}}
