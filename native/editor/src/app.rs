// SPDX-License-Identifier: GPL-3.0-only
//! One native archive/editor window. Media work is lazy and never blocks event handling.
#[path = "gsr_controls.rs"]
mod gsr_controls;
use gsr_controls::{Action as GsrAction, Snapshot as CaptureState};
use crate::{
    ipc,
    jobs::{Event, Jobs},
    model::{self, Document},
    playback::Playback,
};
use anyhow::{Context, Result};
use eframe::egui::{self, Color32, Pos2, Rect, RichText, Sense, Stroke, Vec2};
use serde_json::{Value, json};
use std::{
    collections::{HashSet,BTreeMap},
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
#[derive(Clone)]
struct Recording {
    path: PathBuf,
    name: String,
    created: String,
    duration: f64,
    video: usize,
    audio: usize,
}
enum Message {
    Font(Vec<u8>),
    Opened(u64, Result<Document, String>),
    SourceRates(u64,PathBuf,Value),
    Gsr(GsrAction, Result<Value, String>),
    ScreenshotSaved(Result<PathBuf, String>),
    Library(Vec<Recording>),
    Capture(bool, Result<Value, String>),
    Settings(u64,Result<Value, String>),
    Microphones(Result<Vec<(String,String)>,String>),
    LibrarySaved(String, Result<(), String>),
    File(String, Option<PathBuf>),
    PreviewSaved(Result<(),String>),
}
#[derive(Clone,PartialEq)]
struct UnderlayKey {path:PathBuf,base:u64,position:u64,size:[u32;2]}
struct Studio {
    root: PathBuf,
    cfg: Value,
    settings_polling: bool,
    settings_revision:u64,
    last_settings_poll: Instant,
    gsr_running: Option<bool>,
    gsr_polling: bool,
    gsr_busy: bool,
    last_gsr_poll: Instant,
    library_refresh_due: Option<Instant>,
    instance: ipc::Instance,
    tx: mpsc::Sender<Message>,
    rx: mpsc::Receiver<Message>,
    library: Vec<Recording>,
    filter: String,
    batch_selected: HashSet<PathBuf>,
    batch_mode: bool,
    document: Option<Document>,
    open_generation: u64,
    opening: bool,
    error: String,
    status: Value,
    polling: bool,
    last_poll: Instant,
    jobs: Jobs,
    saver: crate::autosave::Autosave,
    save_errors: BTreeMap<PathBuf, String>,
    close_started: Option<Instant>,
    close_flushed: bool,
    close_token: u64,
    allow_close: bool,
    frame_job: Option<(u64, u64, f64)>,
    job_context:BTreeMap<u64,(PathBuf,u64)>,
    frame_due: Option<Instant>,
    texture: Option<egui::TextureHandle>,
    fast_cache: crate::fast_preview::Cache,
    gpu:Option<std::sync::Arc<crate::gpu_effects::Compositor>>,
    gpu_failed:bool,
    overlays:crate::overlay_cache::Cache,
    preview_settings:crate::preview_settings::PreviewSettings,
    preview_writer:mpsc::Sender<crate::preview_settings::PreviewSettings>,
    playback_base:Option<Value>,
    playback_clock:Option<Instant>,
    playback_first_frame:Option<Instant>,
    playback_frames:u32,
    playback_fps:f64,
    fast_scene: Option<crate::fast_preview::Scene>,
    fast_key: Option<(u64,u64)>,
    underlay:bool,
    underlay_generation:u64,
    playback_sequence:u64,
    underlay_texture:Option<egui::TextureHandle>,
    underlay_cached:Option<UnderlayKey>,
    underlay_pending:Option<(u64,UnderlayKey)>,
    library_open: bool,
    properties_open: bool,
    frame_revision: u64,
    waveforms:BTreeMap<String,(f64,Vec<f32>)>,
    preview: Option<(u64, String)>,
    playback: Option<Playback>,
    want_play: bool,
    tab: String,
    annotation: Option<String>,
    tool: String,
    points: Vec<[f64; 2]>,
    ink: [u8; 3],
    ink_width: f64,
    export_open: bool,
    export_codec: String,
    export_quality: i32,
    advanced_draft: Value,
    advanced_saved: bool,
    export_fast: bool,
    export_preset: String,
    export_audio: bool,
    export_fps: u32,
    export_size: [u32; 2],
    progress: f64,
    settings_open: bool,
    voiceover_open: bool,
    voiceover: Option<crate::voiceover::Voiceover>,
    microphones: Vec<(String,String)>,
    microphone: String,
    microphones_loading: bool,
    voiceover_importing: bool,
    voiceover_error: String,
    library_edit: String,
    startup: Instant,
    passes: u32,
    screenshot: Option<PathBuf>,
    qa_after: f64,
    qa_requested: bool,
    qa_exit: bool,
    qa_actions: bool,
    qa_panel: Option<String>,
    qa_preview:Option<PathBuf>,
    qa_preview_requested:bool,
    qa_position:Option<f64>,
    qa_positions:std::collections::VecDeque<f64>,
    qa_sequence:bool,
    qa_capture_index:u32,
    qa_overlay_steps:u32,
    qa_overlay_target:u32,
    qa_play_seconds:Option<f64>,
    qa_play_started:Option<Instant>,
    output_frame: Option<PathBuf>,
    drag_origin: Option<Vec<(String, f64)>>,
    timeline_zoom:f32,
    timeline_snap:bool,
    crossfade_seconds:f64,
}
fn number(v: &Value, key: &str, default: f64) -> f64 {
    v[key].as_f64().unwrap_or(default)
}
fn timecode(t: f64) -> String {
    let t = t.max(0.) as u64;
    if t >= 3600 {
        format!("{}:{:02}:{:02}", t / 3600, (t / 60) % 60, t % 60)
    } else {
        format!("{}:{:02}", t / 60, t % 60)
    }
}
fn button(ui: &mut egui::Ui, text: &str, primary: bool) -> egui::Response {
    let label=if primary{RichText::new(text).color(Color32::from_rgb(19,31,28))}else{RichText::new(text)};
    let mut b = egui::Button::new(label).min_size(Vec2::new(0., 32.));
    if primary {
        b = b
            .fill(Color32::from_rgb(188, 214, 208))
            .stroke(Stroke::NONE);
    }
    ui.add(b)
}
impl Studio {
    fn new(
        root: PathBuf,
        instance: ipc::Instance,
        open: Option<PathBuf>,
        screenshot: Option<PathBuf>,
        qa_after: f64,
        qa_actions: bool,
        cc: &eframe::CreationContext<'_>,
    ) -> Self {
        let build_started=Instant::now();
        eprintln!("LAYER_UI_BOOT style");
        let mut style = (*cc.egui_ctx.style()).clone();
        style.spacing.item_spacing = Vec2::new(10., 8.);
        style.spacing.button_padding = Vec2::new(12., 7.);
        style.visuals = egui::Visuals::dark();
        style.visuals.panel_fill = Color32::from_rgb(23, 26, 28);
        style.visuals.window_fill = Color32::from_rgb(29, 33, 35);
        style.visuals.extreme_bg_color = Color32::from_rgb(15, 18, 20);
        style.visuals.override_text_color = Some(Color32::from_rgb(232, 237, 238));
        style.visuals.selection.bg_fill = Color32::from_rgb(60, 83, 87);
        style.visuals.widgets.inactive.bg_fill = Color32::from_rgb(40, 45, 48);
        style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(57, 65, 69);
        style.visuals.widgets.active.bg_fill = Color32::from_rgb(66, 80, 84);
        style
            .text_styles
            .insert(egui::TextStyle::Heading, egui::FontId::proportional(22.));
        style
            .text_styles
            .insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
        cc.egui_ctx.set_style(style);
        let (cfg, error) = match ipc::settings() {
            Ok(v) => (v, String::new()),
            Err(e) => (
                json!({"library":"~/Videos/Layer","replay_short":60,"replay_long":300}),
                format!("Could not read recording preferences: {e}"),
            ),
        };
        let library_edit = cfg["library"].as_str().unwrap_or("~/Videos/Layer").into();
        let (tx, rx) = mpsc::channel();
        // Fontconfig can rebuild its cache or access slow mounted font folders.
        // Start with bundled fonts so that this cannot block the first frame.
        let font_tx = tx.clone();
        thread::spawn(move || {
            if let Ok(out) = Command::new("fc-match").args(["-f", "%{file}", "Noto Sans"]).output() {
                if out.status.success() {
                    if let Ok(bytes) = fs::read(String::from_utf8_lossy(&out.stdout).trim()) {
                        let _ = font_tx.send(Message::Font(bytes));
                    }
                }
            }
        });
        let (preview_writer,preview_requests)=mpsc::channel();let preview_results=tx.clone();
        thread::spawn(move||{while let Ok(mut prefs)=preview_requests.recv(){while let Ok(latest)=preview_requests.try_recv(){prefs=latest;}let _=preview_results.send(Message::PreviewSaved(ipc::save_preview_preferences(prefs).map_err(|e|e.to_string())));}});
        let overlays=crate::overlay_cache::Cache::new(root.clone());
        let preview_settings=crate::preview_settings::PreviewSettings::from_config(&cfg).unwrap_or_default();
        let (gpu,error)=match cc.gl.as_ref().map(|gl|crate::gpu_effects::Compositor::new(gl)){
            Some(Ok(gpu))=>(Some(gpu),error),
            Some(Err(message))=>(None,format!("GPU overlays are unavailable; accurate preview is active. {}",message.chars().take(240).collect::<String>())),
            None=>(None,error),
        };
        let mut s = Self {
            root,
            cfg,
            settings_polling: false,
            settings_revision:0,
            last_settings_poll: Instant::now(),
            gsr_running: None,
            gsr_polling: false,
            gsr_busy: false,
            last_gsr_poll: Instant::now(),
            library_refresh_due: None,
            instance,
            tx,
            rx,
            library: vec![],
            filter: String::new(),
            batch_selected:HashSet::new(),batch_mode:false,
            document: None,
            open_generation: 0,
            opening: false,
            error,
            status: json!({}),
            polling: false,
            last_poll: Instant::now() - Duration::from_secs(10),
            jobs: Jobs::new(),
            saver: crate::autosave::Autosave::new(),
            save_errors: BTreeMap::new(),
            close_started: None,
            close_flushed: false,
            close_token: 0,
            allow_close: false,
            frame_job: None,
            job_context:BTreeMap::new(),
            frame_due: None,
            texture: None,
            fast_cache: crate::fast_preview::Cache::new(),
            gpu,gpu_failed:false,overlays,preview_settings,preview_writer,playback_base:None,playback_clock:None,playback_first_frame:None,playback_frames:0,playback_fps:0.,
            fast_scene: None,
            fast_key: None,underlay:false,underlay_generation:0,playback_sequence:0,underlay_texture:None,underlay_cached:None,underlay_pending:None,
            library_open: true,
            properties_open: false,
            frame_revision: u64::MAX,
            waveforms:BTreeMap::new(),
            preview: None,
            playback: None,
            want_play: false,
            tab: "Clip".into(),
            annotation: None,
            tool: "Select".into(),
            points: vec![],
            ink: [241, 104, 105],
            ink_width: 0.004,
            export_open: false,
            export_codec: "libx264".into(),
            export_quality: 20,
            advanced_draft: json!({}),advanced_saved:false,
            export_fast: true,
            export_preset: "fast".into(),
            export_audio: false,
            export_fps: 60,
            export_size: [1920, 1080],
            progress: 0.,
            settings_open: false,
            voiceover_open:false, voiceover:None, microphones:vec![],microphone:"default_input".into(),microphones_loading:false,voiceover_importing:false,voiceover_error:String::new(),
            library_edit,
            startup: Instant::now(),
            passes: 0,
            screenshot,
            qa_after,
            qa_requested: false,
            qa_exit: false,
            qa_actions,
            qa_preview:std::env::args().collect::<Vec<_>>().windows(2).find(|a|a[0]=="--qa-preview").map(|a|PathBuf::from(&a[1])),
            qa_play_seconds:std::env::args().collect::<Vec<_>>().windows(2).find(|a|a[0]=="--qa-play-seconds").and_then(|a|a[1].parse::<f64>().ok()).filter(|t|t.is_finite()&&*t>0.&&*t<=10.),qa_play_started:None,
            qa_preview_requested:false,qa_overlay_steps:0,qa_overlay_target:if std::env::args().any(|a|a=="--qa-overlay-edits"){60}else{0},
            qa_positions:std::env::args().collect::<Vec<_>>().windows(2).find(|a|a[0]=="--qa-positions").map(|a|a[1].split(',').filter_map(|v|v.parse::<f64>().ok()).filter(|t|t.is_finite()&&*t>=0.).take(8).collect()).unwrap_or_default(),
            qa_sequence:std::env::args().any(|a|a=="--qa-positions"),qa_capture_index:0,
            qa_position:std::env::args().collect::<Vec<_>>().windows(2).find(|a|a[0]=="--qa-position").and_then(|a|a[1].parse::<f64>().ok()).filter(|t|t.is_finite()&&*t>=0.),
            qa_panel: std::env::args().collect::<Vec<_>>().windows(2).find(|args|args[0]=="--qa-panel").map(|args|args[1].clone()),
            output_frame: None,
            drag_origin: None,
            timeline_zoom:1.,timeline_snap:true,crossfade_seconds:0.5,
        };
        s.advanced_draft=s.cfg.get("capture_advanced").cloned().unwrap_or_else(||json!({}));
        s.export_codec = s.cfg["export_codec"].as_str().unwrap_or("libx264").into();
        eprintln!("LAYER_UI_BOOT initialized_ms={}",build_started.elapsed().as_millis());
        s.scan();
        s.refresh_settings();
        if let Some(path) = open {
            s.open(&path);
        }
        s
    }
    fn queue_waveforms(&mut self){
        for(id,op,_)in &self.jobs.running{if op=="waveforms"{self.jobs.cancel(*id);}}
        if let Some(doc)=&self.document{
            let id=self.jobs.start_after(&self.root,"waveforms",json!({"path":doc.path}),self.saver.barrier(doc.path.clone()));self.job_context.insert(id,(doc.path.clone(),doc.revision));
        }
    }
    fn gsr_program(&self)->PathBuf {
        match self.cfg["gsr_binary"].as_str(){Some(value) if value!="auto"=>PathBuf::from(value),_=>self.root.join("bin/gpu-screen-recorder")}
    }
    fn open_voiceover(&mut self){
        if self.opening||self.jobs.busy(){self.error="Wait until the current operation finishes.".into();return;}
        self.playback=None;self.voiceover_open=true;self.voiceover=None;self.voiceover_error.clear();self.microphones.clear();self.microphones_loading=true;
        let tx=self.tx.clone();let program=self.gsr_program();
        thread::spawn(move||{let _=tx.send(Message::Microphones(crate::voiceover::devices(&program).map_err(|e|e.to_string())));});
    }
    fn voiceover_dialog(&mut self,ctx:&egui::Context){
        if !self.voiceover_open{return;}
        let mut action="";
        let modal=egui::Modal::new(egui::Id::new("voiceover")).show(ctx,|ui|{
            ui.set_width(390.);ui.heading("Record voiceover");ui.label("The audio clip will start at the current playhead.");ui.add_space(12.);
            if self.microphones_loading{ui.spinner();ui.label("Finding microphones…");}
            egui::ComboBox::from_id_salt("microphone").selected_text(self.microphones.iter().find(|(id,_)|id==&self.microphone).map(|(_,name)|name.as_str()).unwrap_or("Choose a microphone")).show_ui(ui,|ui|{for(id,name)in &self.microphones{ui.selectable_value(&mut self.microphone,id.clone(),name);}});
            if let Some(session)=&self.voiceover{
                if let Some(capture)=&session.ready{ui.label(format!("Recorded {} · ready to add",timecode(capture.duration)));}
                else if let Some(error)=&session.error{ui.colored_label(Color32::from_rgb(255,185,174),error);}
                else{ui.label(if session.stopping{"Finishing recording…".into()}else{format!("Recording microphone · {}",timecode(session.elapsed()))});}
            }
            if !self.voiceover_error.is_empty(){ui.colored_label(Color32::from_rgb(255,185,174),&self.voiceover_error);}
            ui.add_space(12.);ui.horizontal(|ui|{
                if self.voiceover.is_none(){if ui.add_enabled(!self.microphones_loading&&!self.microphones.is_empty(),egui::Button::new("Record")).clicked(){action="start";}}
                else if let Some(session)=&self.voiceover{
                    if session.ready.is_some(){if ui.add_enabled(!self.voiceover_importing,egui::Button::new("Add voiceover")).clicked(){action="add";}}
                    else if session.error.is_none()&&ui.add_enabled(!session.stopping,egui::Button::new("Stop")).clicked(){action="stop";}
                }
                if ui.add_enabled(!self.voiceover_importing,egui::Button::new("Cancel")).clicked(){action="cancel";}
                if self.voiceover_importing{ui.spinner();ui.label("Adding audio…");}
            });
        });
        if modal.should_close()&&!self.voiceover_importing{action="cancel";}
        match action{
            "start"=>{self.voiceover=Some(crate::voiceover::Voiceover::start(self.gsr_program(),self.microphone.clone(),ipc::cache()));},
            "stop"=>{if let Some(session)=&mut self.voiceover{session.finish();}},
            "add"=>{if let Some(file)=self.voiceover.as_ref().and_then(|s|s.ready.as_ref()).map(|c|c.file.clone()){self.voiceover_importing=true;self.media_job("import",json!({"source":file,"overlay":true}));}},
            "cancel"=>{self.voiceover_open=false;self.voiceover=None;},_=>()
        }
    }
    fn refresh_settings(&mut self) {
        if self.settings_polling { return; }
        self.settings_polling=true;self.last_settings_poll=Instant::now();
        let tx=self.tx.clone();let root=self.root.clone();let revision=self.settings_revision;
        thread::spawn(move||{let _=tx.send(Message::Settings(revision,ipc::resolved_settings(&root).map_err(|e|e.to_string())));});
    }
    fn scan(&self) {
        let root = ipc::expand(self.cfg["library"].as_str().unwrap_or("~/Videos/Layer"));
        let tx = self.tx.clone();
        thread::spawn(move || {
            let mut rows = vec![];
            fn walk(dir: &Path, depth: u8, rows: &mut Vec<Recording>) {
                if depth > 4 || rows.len() > 20000 {
                    return;
                }
                let Ok(entries) = fs::read_dir(dir) else {
                    return;
                };
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.is_dir() {
                        if path
                            .file_name()
                            .is_some_and(|n| n == "media" || n == ".staging")
                        {
                            continue;
                        }
                        walk(&path, depth + 1, rows);
                    } else if path.file_name().is_some_and(|n| n == "project.katman") {
                        if let Ok(p) = fs::read(&path).and_then(|b| {
                            serde_json::from_slice::<Value>(&b).map_err(std::io::Error::other)
                        }) {
                            if p["format"] != "katman" {
                                continue;
                            }
                            let sources = p["sources"].as_array();
                            rows.push(Recording {
                                path,
                                name: p["name"].as_str().unwrap_or("Recording").into(),
                                created: p["created"].as_str().unwrap_or("").replace('T', " "),
                                duration: number(&p, "recorded_duration", number(&p, "duration", 0.)),
                                video: sources
                                    .map(|v| v.iter().filter(|s| s["type"] == "video").count())
                                    .unwrap_or(0),
                                audio: sources
                                    .map(|v| v.iter().filter(|s| s["type"] == "audio").count())
                                    .unwrap_or(0),
                            });
                        }
                    }
                }
            }
            walk(&root, 0, &mut rows);
            rows.sort_by(|a, b| b.created.cmp(&a.created));
            let _ = tx.send(Message::Library(rows));
        });
    }
    fn open(&mut self, path: &Path) {
        if self.voiceover_open{self.voiceover_error="Finish or cancel the voiceover before opening another recording.".into();return;}
        for(id,op,_)in &self.jobs.running{if op=="waveforms"{self.jobs.cancel(*id);}}
        self.open_generation += 1;
        self.opening = true;
        let generation = self.open_generation;
        let path = path.to_path_buf();
        let tx = self.tx.clone();
        let saved = self.saver.barrier(path.clone());
        thread::spawn(move || {
            let result = saved.recv().map_err(|_| "The edit save service stopped.".to_string())
                .and_then(|result| result)
                .and_then(|()| Document::load(&path).map_err(|e| format!("{e:#}")));
            let rates=result.as_ref().ok().map(|doc|(doc.path.clone(),json!({"sources":doc.project["sources"]})));
            let _ = tx.send(Message::Opened(generation, result));
            if let Some((path,mut project))=rates{let _=crate::media_rate::enrich_missing_rates(&mut project,&path);let _=tx.send(Message::SourceRates(generation,path,project));}
        });
    }
    fn opened(&mut self, result: Result<Document, String>) {
        self.opening = false;
        match result {
            Ok(mut doc) => {
                if let Some(t)=self.qa_positions.pop_front().or(self.qa_position){doc.position=t.min(model::duration(&doc.model));}
                self.playback = None;
                self.preview = None;
                self.texture = None;
                self.annotation = None;
                self.frame_revision = u64::MAX;
                self.export_size = [
                    number(&doc.model, "width", 1920.) as u32,
                    number(&doc.model, "height", 1080.) as u32,
                ];
                self.export_fps = number(&doc.model, "fps", 60.) as u32;
                if let Some(id)=doc.model["tracks"].as_array().and_then(|tracks|tracks.iter().find(|t|t["kind"]=="video")).and_then(|t|t["clips"].as_array()).and_then(|c|c.first()).and_then(|c|c["id"].as_str()).map(str::to_string){doc.selected.insert(id.clone());doc.primary=Some(id);}
                self.waveforms.clear();
                let id=self.jobs.start_after(&self.root,"waveforms",json!({"path":doc.path}),self.saver.barrier(doc.path.clone()));self.job_context.insert(id,(doc.path.clone(),doc.revision));
                self.document = Some(doc);
                if let Some(gpu)=&self.gpu{gpu.invalidate_cached();}
                self.fast_cache.clear();self.overlays.clear();self.playback_base=None;self.underlay=false;self.underlay_texture=None;self.underlay_cached=None;self.underlay_pending=None;self.fast_scene=None;self.fast_key=None;
                self.library_open=false;
                self.frame_due = Some(Instant::now() + Duration::from_millis(150));
            }
            Err(e) => self.error = format!("Could not open this recording: {e:#}"),
        }
    }
    fn gsr(&mut self, action: GsrAction) {
        if self.gsr_busy { return; }
        self.gsr_busy = true;
        let root = self.root.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = if action == GsrAction::GsrSettings {
                ipc::open_gsr(&root).map(|_| json!({"ok":true}))
            } else {
                ipc::gsr_action(&root, action.command())
            }
            .map_err(|e| format!("{e:#}"));
            let _ = tx.send(Message::Gsr(action, result));
        });
    }
    fn poll_gsr(&mut self) {
        if self.gsr_polling { return; }
        self.gsr_polling = true;
        self.last_gsr_poll = Instant::now();
        let root = self.root.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = ipc::gsr_action(&root, GsrAction::Status.command()).map_err(|e| format!("{e:#}"));
            let _ = tx.send(Message::Gsr(GsrAction::Status, result));
        });
    }
    fn capture(&mut self, action: &str, seconds: Option<u64>) {
        let root = self.root.clone();
        let tx = self.tx.clone();
        let action = action.to_string();
        self.polling = true;
        thread::spawn(move || {
            let result = (|| -> Result<Value> {
                if action != "status" {
                    ipc::ensure_core(&root)?;
                }
                let mut message = json!({"action":action});
                if let Some(seconds) = seconds {
                    message["seconds"] = json!(seconds);
                }
                ipc::request(message)
            })();
            let _ = tx.send(Message::Capture(action != "status", result.map_err(|e| format!("{e:#}"))));
        });
    }
    fn choose(&self, kind: &str, save: bool) {
        let kind = kind.to_string();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let mut command = Command::new("zenity");
            command
                .arg("--file-selection")
                .args(["--title", if save { "Save file" } else { "Open media" }]);
            if kind=="batch_export" {command.arg("--directory");}
            else if save {
                command.args(["--save", "--confirm-overwrite"]);
            }
            let result = command.output().or_else(|_| {
                let mut c = Command::new("kdialog");
                c.arg(if kind=="batch_export" { "--getexistingdirectory" } else if save {
                    "--getsavefilename"
                } else {
                    "--getopenfilename"
                });
                c.output()
            });
            let file = result
                .ok()
                .filter(|o| o.status.success())
                .and_then(|o| String::from_utf8(o.stdout).ok())
                .map(|s| PathBuf::from(s.trim()))
                .filter(|p| !p.as_os_str().is_empty());
            let _ = tx.send(Message::File(kind, file));
        });
    }
    fn underlay_key(&self)->Option<UnderlayKey>{
        let doc=self.document.as_ref()?;
        Some(UnderlayKey{path:doc.path.clone(),base:crate::fast_preview::base_fingerprint(&doc.model)?,position:doc.position.to_bits(),size:self.preview_settings.size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32])})
    }
    fn changed(&mut self) {
        let keep_playing=self.playback.is_some()&&self.playback_base.as_ref().is_some_and(|base|self.document.as_ref().and_then(|doc|crate::fast_preview::base_fingerprint(&doc.model))==crate::fast_preview::base_fingerprint(base));
        if !keep_playing{self.playback=None;self.playback_base=None;}
        self.fast_key=None;
        let keep_underlay=self.underlay_pending.as_ref().is_some_and(|(_,key)|self.underlay_key().as_ref()==Some(key));
        if keep_underlay{
            if let (Some((id,rev,_)),Some(doc))=(&mut self.frame_job,&self.document){*rev=doc.revision;if let Some(context)=self.job_context.get_mut(id){context.1=doc.revision;}}
        }else if let Some((id,_,_))=self.frame_job.take(){self.jobs.cancel(id);self.job_context.remove(&id);self.underlay_pending=None;}
        self.frame_revision=u64::MAX;
        self.preview = None;
        if let Some(doc) = &self.document {
            self.saver.submit(doc.path.clone(), doc.project.clone(), doc.model.clone(), doc.revision);
        }
        self.frame_due = Some(Instant::now() + Duration::from_millis(35));
    }
    fn save_events(&mut self, ctx: &egui::Context) {
        for event in self.saver.results.try_iter().collect::<Vec<_>>() {
            match event {
                crate::autosave::Event::Saved { path, revision: _, result } => match result {
                    Ok(()) => {
                        self.save_errors.remove(&path);
                        if self.save_errors.is_empty() && self.error.starts_with("Edits could not be saved:") {
                            self.error.clear();
                        }
                    }
                    Err(error) => {
                        self.error = format!("Edits could not be saved: {} — {error}", path.display());
                        self.save_errors.insert(path, error);
                    }
                },
                crate::autosave::Event::Flushed(token) => {
                    if self.close_started.is_some() && token == self.close_token {
                        self.close_flushed = true;
                        if self.save_errors.is_empty() {
                            self.allow_close = true;
                            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                        }
                    }
                }
            }
        }
    }
    fn closing(&mut self, ctx: &egui::Context) -> bool {
        if self.allow_close { return false; }
        if ctx.input(|input| input.viewport().close_requested()) {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            if self.close_started.is_none() {
                self.close_started = Some(Instant::now());
                self.close_flushed = false;
                for (id, _, _) in &self.jobs.running { self.jobs.cancel(*id); }
                self.close_token += 1;
                self.saver.flush(self.close_token);
            }
        }
        let Some(started) = self.close_started else { return false; };
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.vertical_centered(|ui| {
                ui.add_space(80.);
                ui.heading(if self.close_flushed { "Some edits could not be saved" } else { "Saving your edits…" });
                ui.add_space(12.);
                if !self.close_flushed { ui.spinner(); }
                for (path, error) in &self.save_errors {
                    ui.label(format!("{}: {error}", path.display()));
                }
                if self.close_flushed || started.elapsed() > Duration::from_secs(5) {
                    ui.label("Closing without saving may lose recent edits.");
                    ui.add_space(12.);
                    if self.close_flushed && ui.button("Retry saving").clicked() {
                        self.close_flushed = false;
                        self.close_token += 1;
                        self.saver.flush(self.close_token);
                    }
                    if ui.button("Continue editing").clicked() {
                        self.close_started = None;
                    }
                    if ui.button("Close without saving").clicked() {
                        self.allow_close = true;
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                }
            });
        });
        ctx.request_repaint_after(Duration::from_millis(50));
        true
    }
    fn export_options(&self,allow_fast:bool)->Value{
        let fast=allow_fast&&self.export_fast&&!self.export_audio&&self.document.as_ref().is_some_and(|d|crate::fast_export::plan(&d.project,&d.model).is_ok());
        let mut options=json!({"codec":self.export_codec,"quality":self.export_quality,"audio_only":self.export_audio,"fps":self.export_fps,"size":self.export_size,"fast":fast});
        if self.export_codec.ends_with("_nvenc"){options["preset"]=json!(match self.export_preset.as_str(){"ultrafast"=>"p1","slow"=>"p7",_=>"p4"});}
        else if self.export_codec=="libaom-av1"{options["cpu_used"]=json!(match self.export_preset.as_str(){"ultrafast"=>8,"slow"=>4,_=>6});}
        else if !self.export_codec.ends_with("_vaapi"){options["preset"]=json!(self.export_preset);}
        options
    }
    fn media_job(&mut self, op: &str, mut request: Value) {
        if self.jobs.busy(){self.error="Finish or cancel the current media operation first.".into();return;}
        if let Some(doc) = &self.document {
            request["path"] = json!(doc.path);
            request["model"] = doc.model.clone();
            request["position"] = json!(doc.position);
            self.progress = 0.;
            let id=self.jobs.start_after(&self.root, op, request, self.saver.barrier(doc.path.clone()));
            self.job_context.insert(id,(doc.path.clone(),doc.revision));
        }
    }
    fn refresh_fast_scene(&mut self,ctx:&egui::Context) {
        self.fast_cache.poll(ctx);self.overlays.poll(ctx);
        if let Some(error)=self.gpu.as_ref().and_then(|gpu|gpu.take_error()){self.gpu_failed=true;self.error=error;self.texture=None;self.frame_revision=u64::MAX;self.fast_key=None;self.playback=None;self.playback_base=None;self.frame_due=Some(Instant::now());}
        if let Some(doc)=&self.document{
            let key=(doc.revision,doc.position.to_bits());
            if self.fast_key!=Some(key){
                let gpu=self.gpu.is_some()&&!self.gpu_failed;
                self.fast_scene=if self.playback.is_some(){
                    if self.playback_base.is_some()&&gpu{crate::fast_preview::overlay_scene_at(&doc.model,doc.position,self.preview_settings.size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32]))}else{None}
                }else{
                    let source_bounds=self.preview_settings.bounds();
                    let scene=crate::fast_preview::scene_with_options(&doc.path,&doc.project,&doc.model,doc.position,source_bounds,gpu);
                    self.underlay=scene.is_none()&&gpu&&crate::fast_preview::base_fingerprint(&doc.model).is_some();
                    if self.underlay{let overlay=crate::fast_preview::overlay_scene_at(&doc.model,doc.position,self.preview_settings.size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32]));if overlay.is_none(){self.underlay=false;}overlay}else{scene}
                };
                self.fast_key=Some(key);
                if self.playback.is_some()&&self.playback_base.is_some()&&self.fast_scene.is_none(){
                    self.playback=None;self.playback_base=None;self.underlay=false;self.texture=None;self.frame_revision=u64::MAX;self.frame_due=Some(Instant::now());
                    self.error="This overlay needs the accurate preview. Playback stopped while it is prepared.".into();
                }
                if let Some(scene)=&self.fast_scene{
                    if self.playback.is_none(){self.fast_cache.request(scene);}

                }
            }
            if let Some(scene)=&self.fast_scene{
                let texts=crate::fast_preview::text_keys(scene);let captions=crate::fast_preview::caption_keys(scene);
                let raster=self.preview_settings.size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32]);let upcoming=crate::fast_preview::upcoming_captions(&doc.model,doc.position,raster);
                self.overlays.request_layers(&texts,&captions,&upcoming);
                if self.fast_cache.failed(scene)||(texts.iter().any(|key|self.overlays.failed(key))||captions.iter().any(|key|self.overlays.caption_failed(key))){self.fast_scene=None;self.underlay=false;self.texture=None;self.frame_revision=u64::MAX;self.frame_due=Some(Instant::now());self.playback=None;self.playback_base=None;}
                else if !self.fast_cache.ready(scene)||(texts.iter().any(|key|!self.overlays.ready(key))||captions.iter().any(|key|!self.overlays.caption_ready(key))){ctx.request_repaint_after(Duration::from_millis(4));}
            }
        }
    }
    fn queue_frame(&mut self) {
        if self.output_frame.is_none() && self.fast_scene.is_some()&&!self.underlay{self.frame_due=None;return;}
        if self.frame_job.is_some() {
            return;
        }
        let underlay=if self.underlay&&self.output_frame.is_none(){self.underlay_key()}else{None};
        if underlay.is_some()&&underlay==self.underlay_cached&&self.underlay_texture.is_some(){self.frame_due=None;return;}
        if let Some(doc) = &self.document {
            let base_model=if underlay.is_some(){crate::fast_preview::playback_base(&doc.model)}else{None};
            let mut project=doc.project.clone();
            if let Some(object)=project.as_object_mut(){object.remove("studio");}
            let id = self.jobs.start(
                &self.root,
                "frame",
                json!({"path":doc.path,"project_snapshot":project,"model":base_model.as_ref().unwrap_or(&doc.model),"position":doc.position,"size":if self.output_frame.is_some(){crate::preview_settings::PreviewSettings::default().size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32])}else{self.preview_settings.size([number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32])},"fps":self.preview_settings.fps(&doc.model,&doc.project)}),
            );
            self.job_context.insert(id,(doc.path.clone(),doc.revision));
            self.frame_job = Some((id, doc.revision, doc.position));
            self.underlay_pending=underlay.map(|key|(id,key));
            self.frame_due = None;
        }
    }
    fn load_texture(&mut self, ctx: &egui::Context, file: &str) {
        match image::open(file) {
            Ok(image) => {
                let rgba = image.into_rgba8();
                let size = [rgba.width() as usize, rgba.height() as usize];
                self.texture = Some(ctx.load_texture(
                    "Preview",
                    egui::ColorImage::from_rgba_unmultiplied(size, &rgba),
                    egui::TextureOptions::LINEAR,
                ));
            }
            Err(e) => self.error = format!("Preview image could not be decoded: {e}"),
        }
    }
    fn events(&mut self, ctx: &egui::Context) {
        if let Some(session)=&mut self.voiceover{session.poll();}
        for incoming in self.instance.incoming.try_iter().collect::<Vec<_>>() {
            if let Some(path) = incoming {
                self.open(&path);
            }
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        }
        for message in self.rx.try_iter().collect::<Vec<_>>() {
            match message {
                Message::Font(bytes) => {
                    let mut fonts = egui::FontDefinitions::default();
                    fonts.font_data.insert("Noto Sans".into(), egui::FontData::from_owned(bytes).into());
                    fonts.families.get_mut(&egui::FontFamily::Proportional).unwrap().insert(0, "Noto Sans".into());
                    ctx.set_fonts(fonts);
                }
                Message::SourceRates(generation,path,project)=>{
                    let previous=self.document.as_ref().map(|doc|self.preview_settings.fps(&doc.model,&doc.project));
                    if generation==self.open_generation&&let Some(doc)=&mut self.document&&doc.path==path{
                        for source in doc.project["sources"].as_array_mut().into_iter().flatten(){if let Some(probed)=project["sources"].as_array().and_then(|rows|rows.iter().find(|row|row["id"]==source["id"]&&row["file"]==source["file"])){
                            for key in ["avg_frame_rate","r_frame_rate"]{if source.get(key).is_none()&&let Some(value)=probed.get(key){source[key]=value.clone();}}
                        }}
                    }
                    let current=self.document.as_ref().map(|doc|self.preview_settings.fps(&doc.model,&doc.project));
                    if generation==self.open_generation&&previous!=current&&self.preview_settings.frame_rate==crate::preview_settings::FrameRate::Source&&self.playback.is_some(){self.playback=None;self.playback_base=None;self.play(ctx);}
                }
                Message::Opened(generation, result) => {
                    if generation == self.open_generation { self.opened(result); }
                }
                Message::Gsr(action, result) => {
                    if action == GsrAction::Status {
                        self.gsr_polling = false;
                        self.gsr_running = Some(result.is_ok());
                    } else {
                        self.gsr_busy = false;
                        match result {
                            Ok(_) => {
                                self.gsr_running = Some(true);
                                self.last_gsr_poll = Instant::now() - Duration::from_secs(10);
                                if action.is_replay_save() {
                                    // GSR writes the replay after acknowledging the request.
                                    // Refresh the archive after that asynchronous save settles.
                                    self.library_refresh_due = Some(Instant::now() + Duration::from_millis(1800));
                                }
                            }
                            Err(error) => self.error = error,
                        }
                    }
                }
                Message::ScreenshotSaved(result) => {
                    match result {
                        Ok(path) => println!("LAYER_NATIVE_SINGLE_WINDOW_SCREENSHOT_OK {}", path.display()),
                        Err(error) => eprintln!("Screenshot: {error}"),
                    }
                    self.qa_exit = true;
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
                Message::Library(rows) => self.library = rows,
                Message::Microphones(result)=>{self.microphones_loading=false;match result{Ok(devices)=>{self.microphones=devices;if !self.microphones.iter().any(|(id,_)|id==&self.microphone){if let Some((id,_))=self.microphones.first(){self.microphone=id.clone();}else{self.voiceover_error="No microphone is available. Check your desktop sound settings.".into();}}},Err(error)=>self.voiceover_error=error}},
                Message::PreviewSaved(result)=>{if let Err(error)=result{self.error=format!("Could not save preview settings: {error}");}},
                Message::LibrarySaved(library, result) => {
                    match result { Ok(())=>{self.settings_revision+=1;self.cfg["library"]=json!(library);self.scan();},Err(error)=>self.error=error }
                }
                Message::Settings(revision,result) => {
                    self.settings_polling=false;
                    if revision!=self.settings_revision{continue;}
                    match result {
                        Ok(cfg)=>{let changed=self.cfg["library"]!=cfg["library"];if !self.settings_open||self.library_edit==self.cfg["library"].as_str().unwrap_or(""){self.library_edit=cfg["library"].as_str().unwrap_or("~/Videos/Layer").into();}let preview=crate::preview_settings::PreviewSettings::from_config(&cfg).unwrap_or_default();if preview!=self.preview_settings{self.preview_settings=preview;self.fast_key=None;self.frame_due=Some(Instant::now());}self.cfg=cfg;if changed{self.scan();}},
                        Err(error)=>self.error=format!("Recording preferences: {error}"),
                    }
                }
                Message::Capture(is_action, result) => {
                    self.polling = false;
                    match result {
                        Ok(v) => {
                            if v.is_object() {
                                let last = self.status["last_project"].clone();
                                self.status = v;
                                if self.status["last_project"] != last {
                                    self.scan();
                                }
                                if let Some(error) = self.status["errors"]
                                    .as_array()
                                    .and_then(|a| a.last())
                                    .and_then(Value::as_str)
                                {
                                    self.error = error.into();
                                }
                            } else {
                                self.scan();
                            }
                        }
                        Err(e) => {
                            if is_action || self.status["running"] == true {
                                self.error = e;
                            }
                        }
                    }
                }
                Message::File(kind, Some(file)) => match kind.as_str() {
                    "open" => self.open(&file),
                    "append" | "overlay" => {
                        self.media_job("import", json!({"source":file,"overlay":kind=="overlay"}))
                    }
                    "batch_export"=>{
                        let paths:Vec<_>=self.batch_selected.iter().map(|path|path.to_string_lossy().into_owned()).collect();
                        self.progress=0.;self.jobs.start_after(&self.root,"batch_export",json!({"paths":paths,"output_directory":file,"options":self.export_options(false)}),self.saver.barrier_all(self.batch_selected.iter().cloned().collect()));self.export_open=false;
                    }
                    "export" => {
                        if file.exists() {
                            self.error =
                                "Choose a new filename to preserve the existing export.".into();
                        } else {
                            self.media_job("export",json!({"output":file,"options":self.export_options(true)}));
                            self.export_open = false;
                        }
                    }
                    "lut" => {
                        if let Some(doc) = &mut self.document {
                            doc.checkpoint();
                            if let Some(clip) = doc.selected_clip_mut() {
                                clip["lut"] = json!(file);
                            }
                            doc.revision += 1;
                            self.changed();
                        }
                    }
                    "import_srt" => self.media_job("import_srt", json!({"file":file})),
                    "export_srt" => self.media_job("export_srt", json!({"output":file})),
                    "frame" => {
                        self.output_frame = Some(file);
                        self.queue_frame();
                    }
                    _ => (),
                },
                Message::File(_, None) => (),
            }
        }
        for event in self.jobs.rx.try_iter().collect::<Vec<_>>() {
            match event {
                Event::Progress(id, value) => {if self.jobs.running.iter().any(|(n,op,_)|*n==id&&op!="frame"&&op!="waveforms"){self.progress = value;}},
                Event::Done(id, op, result) => {
                    self.jobs.finished(id);
                    let frame = self.frame_job.filter(|(n, _, _)| *n == id);
                    if frame.is_some() {
                        self.frame_job = None;
                    }
                    let context=self.job_context.remove(&id);
                    let same_document=context.as_ref().is_some_and(|(path,_)|self.document.as_ref().is_some_and(|doc|&doc.path==path));
                    let current=context.as_ref().is_some_and(|(path,rev)|self.document.as_ref().is_some_and(|doc|&doc.path==path&&doc.revision==*rev));
                    if op=="waveforms"&&!same_document{continue;}
                    if !current && !["export","export_srt","proxies","batch_export","waveforms","capture_settings"].contains(&op.as_str()){
                        if op=="frame"{self.frame_due=Some(Instant::now());}else{self.want_play=false;self.error="The recording changed while this operation ran. Your newer edits were kept; run the operation again.".into();}continue;
                    }
                    match result {
                        Ok(data) => match op.as_str() {
                            "capture_settings"=>{self.cfg["capture_advanced"]=data["capture_advanced"].clone();self.advanced_saved=true;},
                            "waveforms"=>{for item in data["items"].as_array().into_iter().flatten(){if let (Some(id),Some(seconds),Some(peaks))=(item["source_id"].as_str(),item["duration"].as_f64(),item["peaks"].as_array()){if seconds>0.{self.waveforms.insert(id.into(),(seconds,peaks.iter().map(|v|v.as_f64().unwrap_or(0.) as f32).collect()));}}}},
                            "frame" => {
                                if let Some(file) = data["file"].as_str().or_else(|| data.as_str())
                                {
                                    if let Some(target) = self.output_frame.take() {
                                        if target.exists() {
                                            self.error="Choose a new filename to preserve the existing image.".into();
                                        } else if let Err(e) = fs::copy(file, target) {
                                            self.error = e.to_string();
                                        }
                                    }
                                    if let (Some((_, rev, pos)), Some(doc)) =
                                        (frame, &self.document)
                                    {
                                        if rev == doc.revision && (pos - doc.position).abs() < 0.05
                                        {
                                            let underlay=self.underlay_pending.take().filter(|(job,_)|*job==id).map(|(_,key)|key);
                                            if let Some(key)=underlay{
                                                if self.underlay_key().as_ref()==Some(&key){
                                                    match image::open(file){Ok(image)=>{let pixels=image.to_rgba8();let size=[pixels.width()as usize,pixels.height()as usize];let image=egui::ColorImage::from_rgba_unmultiplied(size,&pixels);if let Some(texture)=&mut self.underlay_texture{texture.set(image,egui::TextureOptions::LINEAR);}else{self.underlay_texture=Some(ctx.load_texture("Cached source composition",image,egui::TextureOptions::LINEAR));}self.underlay_cached=Some(key);self.underlay_generation=self.underlay_generation.wrapping_add(1);},Err(error)=>self.error=error.to_string()}
                                                }
                                                self.frame_due=Some(Instant::now());
                                            }else{
                                                self.load_texture(ctx,file);self.frame_revision=rev;
                                            }
                                        } else {
                                            self.frame_due = Some(Instant::now());
                                        }
                                    }
                                }
                            }
                            "preview" => {
                                if let Some(file) = data["file"].as_str().or_else(|| data.as_str())
                                {
                                    if let Some(doc) = &self.document {
                                        self.preview = Some((doc.revision, file.into()));
                                        if self.want_play {
                                            match Playback::start(file, doc.position, [number(&doc.model,"width",1920.) as u32,number(&doc.model,"height",1080.) as u32]) {
                                                Ok(p) => self.playback = Some(p),
                                                Err(e) => self.error = e.to_string(),
                                            };
                                            self.want_play = false;
                                        }
                                    }
                                }
                            }
                            "import" => {
                                if self.voiceover_importing{self.voiceover_importing=false;self.voiceover_open=false;self.voiceover=None;}
                                if let Some(doc) = &mut self.document {
                                    doc.checkpoint();
                                    doc.project = data["project"].clone();
                                    doc.model = data["model"].clone();
                                    doc.revision += 1;
                                    self.changed();
                                }
                                self.queue_waveforms();
                            }
                            "transcribe" | "import_srt" => {
                                if let Some(doc) = &mut self.document {
                                    doc.checkpoint();
                                    doc.model["subtitles"] = data["subtitles"]
                                        .as_array()
                                        .cloned()
                                        .map(Value::Array)
                                        .unwrap_or(data);
                                    doc.revision += 1;
                                    self.tab = "Captions".into();
                                    self.changed();
                                }
                            }
                            "stabilize" => {
                                if let Some(doc) = &mut self.document {
                                    doc.checkpoint();
                                    if let Some(clip) = doc.selected_clip_mut() {
                                        clip["stabilization"] = data["file"].clone();
                                    }
                                    doc.revision += 1;
                                    self.changed();
                                }
                            }
                            "track" => {
                                if let (Some(doc), Some(selected)) =
                                    (&mut self.document, &self.annotation)
                                {
                                    doc.checkpoint();
                                    if let Some(ann) = doc.model["annotations"]
                                        .as_array_mut()
                                        .and_then(|a| a.iter_mut().find(|a| a["id"] == *selected))
                                    {
                                        ann["keys"] = data["keys"]
                                            .as_object()
                                            .cloned()
                                            .map(Value::Object)
                                            .unwrap_or(data);
                                    }
                                    doc.revision += 1;
                                    self.changed();
                                }
                            }
                            _ => self.progress = 1.,
                        },
                        Err(e) => {
                            if self.voiceover_importing&&op=="import"{self.voiceover_importing=false;self.voiceover_error=e.clone();}
                            if op!="waveforms"&&(op != "frame" || self.document.is_some()) {
                                self.error = e;
                            }
                        }
                    }
                }
            }
        }
        if let Some(playback) = &mut self.playback {
            if let Some(message)=playback.diagnostics.try_iter().last(){self.error=format!("Preview: {message}");}
            let mut latest = None;
            for frame in playback.frames.try_iter() {
                latest = Some(frame);
            }
            if let Some(frame) = latest {
                self.playback_first_frame.get_or_insert_with(Instant::now);self.playback_frames+=1;self.playback_sequence=self.playback_sequence.wrapping_add(1);
                let clock=self.playback_clock.get_or_insert_with(Instant::now);
                if clock.elapsed().as_secs_f64()>=0.5{self.playback_fps=self.playback_frames as f64/clock.elapsed().as_secs_f64();self.playback_frames=0;*clock=Instant::now();}
                let image=egui::ColorImage::from_rgba_unmultiplied([frame.width,frame.height],&frame.rgba);
                if let Some(texture)=&mut self.texture{texture.set(image,egui::TextureOptions::LINEAR);}else{self.texture=Some(ctx.load_texture("Playing",image,egui::TextureOptions::LINEAR));}
                if let Some(doc) = &mut self.document {
                    doc.position = frame.time;
                }
            }
            if self.document.is_some() {
                if self.playback.as_ref().is_some_and(|p|p.finished.load(std::sync::atomic::Ordering::Acquire)) {
                    self.playback = None;self.playback_base=None;self.fast_key=None;
                }
            }

        }
        self.refresh_fast_scene(ctx);
        if self.frame_due.is_some_and(|time| Instant::now() >= time)
            && self.playback.is_none()
        {
            self.queue_frame();
        }
        if self.last_settings_poll.elapsed() > Duration::from_secs(5) && !self.settings_polling { self.refresh_settings(); }
        if self.last_gsr_poll.elapsed() > Duration::from_secs(10) && !self.gsr_polling { self.poll_gsr(); }
        if self.library_refresh_due.is_some_and(|due| Instant::now() >= due) {
            self.library_refresh_due = None;
            self.scan();
            self.last_poll = Instant::now() - Duration::from_secs(2);
        }
        if self.last_poll.elapsed() > Duration::from_millis(800) && !self.polling {
            self.capture("status", None);
            self.last_poll = Instant::now();
        }
    }
    fn toolbar(&mut self, ctx: &egui::Context) {
        let compact=ctx.viewport_rect().width()<1000.;let state=CaptureState::from_status(&self.status);let mut action=None;
        egui::TopBottomPanel::top("recording").exact_height(52.).show(ctx,|ui|{
            ui.add_space(6.);ui.horizontal(|ui|{
                ui.add_space(8.);ui.label(RichText::new(if compact {"Layer"}else{"Layer editor"}).size(18.).strong());ui.add_space(12.);
                if ui.selectable_label(self.library_open||self.document.is_none(),"Recordings").on_hover_text("Browse your recordings").clicked(){self.library_open=!self.library_open;}
                ui.separator();
                ui.add_enabled_ui(!self.gsr_busy,|ui|{
                    ui.menu_button("Record & replay",|ui|{
                        ui.set_min_width(230.);
                        if let Some(chosen)=gsr_controls::controls(ui,&state,[self.cfg["replay_short"].as_u64().unwrap_or(60),self.cfg["replay_long"].as_u64().unwrap_or(300)]){action=Some(chosen);ui.close();}
                    });
                    if let Some(chosen)=gsr_controls::capture_menu(ui){action=Some(chosen);}
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center),|ui|{
                    if ui.button("Preferences").clicked(){self.settings_open=!self.settings_open;}
                    if self.document.is_some()&&ui.selectable_label(self.properties_open,"Properties").on_hover_text("Clip timing, picture, audio and effects").clicked(){self.properties_open=!self.properties_open;}
                    if !compact{ui.label(RichText::new(state.summary(true)).small().color(state.tone(ui)));}
                });
            });
        });
        if let Some(chosen)=action{self.gsr(chosen);}
    }
    fn archive(&mut self, ctx: &egui::Context) {
        if self.document.is_some() && !self.library_open{return;}
        egui::SidePanel::left("library")
            .default_width(if ctx.viewport_rect().width()<1100. {200.}else{250.})
            .min_width(205.)
            .max_width(420.)
            .resizable(true)
            .show(ctx, |ui| {
                ui.add_space(14.);
                ui.horizontal(|ui| {
                    ui.heading("Library");
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.small_button("Refresh").clicked() {
                            self.scan();
                        }
                    });
                });
                ui.add(
                    egui::TextEdit::singleline(&mut self.filter)
                        .hint_text("Search recordings")
                        .desired_width(f32::INFINITY),
                );
                ui.add_space(8.);
                let mut open = None;
                egui::ScrollArea::vertical().show(ui, |ui| {
                    for record in &self.library {
                        if !record
                            .name
                            .to_lowercase()
                            .contains(&self.filter.to_lowercase())
                        {
                            continue;
                        }
                        let selected = self
                            .document
                            .as_ref()
                            .is_some_and(|d| d.path == record.path);
                        let frame = egui::Frame::new()
                            .fill(if selected {
                                Color32::from_rgb(44, 60, 65)
                            } else {
                                Color32::TRANSPARENT
                            })
                            .inner_margin(12.);
                        let response = frame
                            .show(ui, |ui| {
                                ui.set_min_width(ui.available_width());
                                ui.horizontal(|ui|{
                                    let mut checked=self.batch_selected.contains(&record.path);
                                    let check=ui.checkbox(&mut checked, "").on_hover_text("Select for batch export");
                                    check.widget_info(||egui::WidgetInfo::selected(egui::WidgetType::Checkbox,ui.is_enabled(),checked,format!("Select {} for batch export",record.name)));
                                    if check.changed(){if checked{self.batch_selected.insert(record.path.clone());}else{self.batch_selected.remove(&record.path);}}
                                    ui.label(RichText::new(&record.name).strong());
                                });
                                ui.label(
                                    RichText::new(
                                        record.created.chars().take(19).collect::<String>(),
                                    )
                                    .small()
                                    .color(Color32::from_gray(166)),
                                );
                                ui.label(
                                    RichText::new(format!(
                                        "{} · {} screens · {} audio",
                                        timecode(record.duration),
                                        record.video,
                                        record.audio
                                    ))
                                    .small()
                                    .color(Color32::from_gray(177)),
                                );
                            })
                            .response;
                        let click =
                            ui.interact(Rect::from_min_max(response.rect.min+Vec2::new(36.,0.),response.rect.max), ui.id().with(&record.path), Sense::click());
                        if click.clicked() {
                            open = Some(record.path.clone());
                        }
                        ui.add_space(3.);
                    }
                });
                if let Some(path) = open {
                    self.open(&path);
                }
                ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
                    if ui.button("Open recording…").clicked() {
                        self.choose("open", false);
                    }
                    if ui.add_enabled(!self.batch_selected.is_empty()&&!self.jobs.busy(),egui::Button::new(format!("Batch export {}…",self.batch_selected.len()))).clicked(){self.batch_mode=true;self.export_open=true;}
                    ui.label(
                        RichText::new(format!("{} recordings", self.library.len()))
                            .small()
                            .color(Color32::from_gray(165)),
                    );
                });
            });
    }
    fn editing_bar(&mut self, ui: &mut egui::Ui) {
        let mut operation = None;
        ui.horizontal_wrapped(|ui| {
            if let Some(doc) = &self.document {
                ui.label(
                    RichText::new(doc.project["name"].as_str().unwrap_or("Recording"))
                        .strong()
                        .size(17.),
                );
            }
            ui.separator();
            ui.menu_button("Add media",|ui|{
                if ui.button("Append recording…").clicked(){operation=Some("append");ui.close();}
                if ui.button("Overlay media…").clicked(){operation=Some("overlay");ui.close();}
                if ui.button("Record voiceover…").clicked(){operation=Some("voiceover");ui.close();}
            });
            ui.menu_button("Edit",|ui|{
                for(label,op)in[("Undo","undo"),("Redo","redo"),("Remove selected","delete")]{if ui.button(label).clicked(){operation=Some(op);ui.close();}}
            });
            if ui.add_enabled(self.document.as_ref().is_some_and(|d|!d.selected.is_empty()),egui::Button::new("Split")).clicked(){operation=Some("split");}
            ui.menu_button("Tools",|ui|{
                if ui.button("Build source proxies…").clicked(){operation=Some("proxies");ui.close();}
                if ui.button("Save current frame…").clicked(){operation=Some("save_frame");ui.close();}
                ui.separator();
                ui.add(egui::DragValue::new(&mut self.crossfade_seconds).range(0.04..=30.).speed(0.05).prefix("Crossfade ").suffix(" s"));
                if ui.add_enabled(self.document.as_ref().is_some_and(|d|d.selected.len()==2),egui::Button::new("Crossfade selected clips")).clicked(){operation=Some("crossfade");ui.close();}
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if button(ui, "Export…", true).clicked() {
                    self.batch_mode=false;self.export_open = true;
                }
            });
        });
        if let Some(op) = operation {
            if op=="voiceover"{self.open_voiceover();return;}
            if op=="proxies"{self.media_job("proxies",json!({}));return;}
            if op=="save_frame"{self.choose("frame",true);return;}
            if op == "append" || op == "overlay" {
                self.choose(op, false);
            } else if let Some(doc) = &mut self.document {
                match op {
                    "undo" => doc.undo(),
                    "redo" => doc.redo(),
                    "split" => {
                        if let Err(e) = doc.split_selected() {
                            self.error = e.to_string();
                        }
                    }
                    "delete" => doc.delete_selected(),
                    "crossfade"=>{if let Err(error)=doc.crossfade_selected(self.crossfade_seconds){self.error=error.to_string();}},
                    _ => (),
                };
                self.changed();
            }
        }
    }
    fn play(&mut self,ctx:&egui::Context) {
        self.fast_key=None;
        if self.playback.is_some(){self.playback=None;self.playback_base=None;self.frame_due=Some(Instant::now());return;}
        let Some(doc)=&self.document else{return;};
        let position=if doc.position>=model::duration(&doc.model)-0.04{0.}else{doc.position};
        let canvas=[number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32];
        let overlay=crate::fast_preview::overlay_scene_at(&doc.model,position,self.preview_settings.size(canvas));
        let healthy=overlay.as_ref().is_some_and(|scene|crate::fast_preview::text_keys(scene).iter().all(|key|!self.overlays.known_failure(key))&&crate::fast_preview::caption_keys(scene).iter().all(|key|!self.overlays.known_caption_failure(key)));
        let base=if self.gpu.is_some()&&!self.gpu_failed&&healthy{crate::fast_preview::playback_base(&doc.model)}else{None};
        match Playback::start_timeline(&self.root,&doc.path,base.as_ref().unwrap_or(&doc.model),&doc.project,position,canvas,self.preview_settings.size(canvas),self.preview_settings.fps(&doc.model,&doc.project)as f64,ctx.clone()){
            Ok(player)=>{self.playback=Some(player);self.playback_base=base;self.playback_clock=None;self.playback_first_frame=None;self.playback_frames=0;self.playback_fps=0.;self.want_play=false;},
            Err(error)=>self.error=error.to_string(),
        }
    }
    fn canvas(&mut self, ui: &mut egui::Ui) {
        let ctx=ui.ctx().clone();
        self.editing_bar(ui);
        ui.add_space(8.);
        let Some(doc) = &self.document else {
            return;
        };
        let canvas_size = Vec2::new(
            number(&doc.model, "width", 1920.) as f32,
            number(&doc.model, "height", 1080.) as f32,
        );
        let source_fps=crate::preview_settings::PreviewSettings{frame_rate:crate::preview_settings::FrameRate::Source,..self.preview_settings}.fps(&doc.model,&doc.project);
        let avail = Vec2::new(
            ui.available_width(),
            (ui.available_height() - (48. + (850. / ui.available_width().max(200.)).ceil() * 40.)).max(80.),
        );
        let scale = (avail.x / canvas_size.x).min(avail.y / canvas_size.y);
        let size = canvas_size * scale;
        let (outer, _) = ui.allocate_exact_size(avail, Sense::hover());
        let rect = Rect::from_center_size(outer.center(), size);
        ui.painter()
            .rect_filled(outer, 0., Color32::from_rgb(10, 12, 14));
        let native_canvas=[canvas_size.x,canvas_size.y];
        let raster=self.preview_settings.size([canvas_size.x as u32,canvas_size.y as u32]);
        let gpu_frame=if self.gpu.is_some()&&!self.gpu_failed{
            self.fast_scene.as_ref().and_then(|scene|{
                if self.playback.is_some()&&self.playback_base.is_some(){
                    let texture=self.texture.as_ref()?;
                    let mut ops=vec![crate::gpu_effects::Op::Mesh(crate::fast_preview::image_mesh(texture.id(),[0.,0.,canvas_size.x,canvas_size.y],Color32::WHITE),None)];
                    ops.extend(crate::fast_preview::mark_ops(&ctx,scene,native_canvas,&self.overlays)?);
                    Some(crate::gpu_effects::Frame{size:raster,canvas:native_canvas,ops,base_count:1,generation:self.playback_sequence})
                }else if self.playback.is_none()&&self.underlay{
                    let key=self.underlay_key()?;if self.underlay_cached.as_ref()!=Some(&key){return None;}
                    let texture=self.underlay_texture.as_ref()?;
                    let mut ops=vec![crate::gpu_effects::Op::Mesh(crate::fast_preview::image_mesh(texture.id(),[0.,0.,canvas_size.x,canvas_size.y],Color32::WHITE),None)];
                    ops.extend(crate::fast_preview::mark_ops(&ctx,scene,native_canvas,&self.overlays)?);
                    Some(crate::gpu_effects::Frame{size:raster,canvas:native_canvas,ops,base_count:1,generation:self.underlay_generation})
                }else if self.playback.is_none(){self.fast_cache.gpu_frame(&ctx,scene,native_canvas,raster,&self.overlays)}else{None}
            })
        }else{None};
        if let Some(frame)=gpu_frame{
            ui.painter().add(egui::Shape::Callback(self.gpu.as_ref().unwrap().callback(rect,frame)));
        }else if self.fast_scene.is_some()&&!self.gpu_failed&&self.gpu.as_ref().is_some_and(|gpu|gpu.has_cached(native_canvas)){
            ui.painter().add(egui::Shape::Callback(self.gpu.as_ref().unwrap().cached_callback(rect,native_canvas)));
            let badge=Rect::from_min_size(rect.left_bottom()+Vec2::new(12.,-32.),Vec2::new(160.,24.));
            ui.painter().rect_filled(badge,3.,Color32::from_black_alpha(220));
            ui.painter().text(badge.center(),egui::Align2::CENTER_CENTER,"Updating preview…",egui::FontId::proportional(12.),Color32::WHITE);
        }else if self.playback.is_none()&&self.fast_scene.is_some()&&self.gpu.is_none(){
            self.fast_cache.paint(ui.painter(),rect,canvas_size,self.fast_scene.as_ref().unwrap());
        }else if self.fast_scene.is_none()&&let Some(texture)=self.texture.as_ref().filter(|_|self.playback.is_some()||self.frame_revision==doc.revision){
            ui.painter().image(texture.id(),rect,Rect::from_min_max(Pos2::ZERO,Pos2::new(1.,1.)),Color32::WHITE);
        }else{
            ui.painter().text(rect.center(),egui::Align2::CENTER_CENTER,"Preparing preview…",egui::FontId::proportional(16.),Color32::from_gray(180));
        }
        let response = ui.interact(rect, ui.id().with("canvas"), Sense::click_and_drag());
        let point = response.interact_pointer_pos().map(|p| {
            [
                ((p.x - rect.left()) / rect.width()).clamp(0., 1.) as f64,
                ((p.y - rect.top()) / rect.height()).clamp(0., 1.) as f64,
            ]
        });
        if self.tool != "Select" && self.playback.is_none() {
            if response.drag_started() {
                self.points.clear();
                if let Some(p) = point {
                    self.points.push(p);
                }
            }
            if response.dragged() {
                if let Some(p) = point {
                    if self.tool == "Pen" {
                        if self.points.len() < 5000 {
                            self.points.push(p);
                        }
                    } else {
                        if self.points.len() > 1 {
                            self.points.pop();
                        }
                        self.points.push(p);
                    }
                }
            }
            if self.points.len() > 1 {
                let points: Vec<Pos2> = self
                    .points
                    .iter()
                    .map(|p| {
                        Pos2::new(
                            rect.left() + p[0] as f32 * rect.width(),
                            rect.top() + p[1] as f32 * rect.height(),
                        )
                    })
                    .collect();
                let stroke = Stroke::new(
                    (self.ink_width * rect.height() as f64) as f32,
                    Color32::from_rgb(self.ink[0], self.ink[1], self.ink[2]),
                );
                if ["Blackout", "Blur", "Pixelate", "Rectangle"].contains(&self.tool.as_str()) {
                    ui.painter().rect_stroke(
                        Rect::from_two_pos(points[0], *points.last().unwrap()),
                        0.,
                        stroke,
                        egui::StrokeKind::Inside,
                    );
                } else {
                    ui.painter().add(egui::Shape::line(points, stroke));
                }
            }
            if response.drag_stopped() && !self.points.is_empty() {
                if let Some(doc) = &mut self.document {
                    doc.checkpoint();
                    let kind = match self.tool.as_str() {
                        "Pen" => "pen",
                        "Line" => "line",
                        "Arrow" => "arrow",
                        "Rectangle" => "rectangle",
                        "Blackout" => "redact",
                        "Blur" => "blur",
                        "Pixelate" => "pixelate",
                        _ => "pen",
                    };
                    let id = model::uid();
                    let mut item = json!({"id":id,"kind":kind,"start":doc.position,"end":model::duration(&doc.model).max(doc.position+0.1),"x":0,"y":0,"w":canvas_size.x,"h":canvas_size.y,"points":self.points,"color":format!("#{:02x}{:02x}{:02x}",self.ink[0],self.ink[1],self.ink[2]),"width":self.ink_width,"keys":{}});
                    if ["redact", "blur", "pixelate"].contains(&kind) {
                        let a = self.points[0];
                        let b = *self.points.last().unwrap();
                        item["x"] = json!(a[0].min(b[0]) * canvas_size.x as f64);
                        item["y"] = json!(a[1].min(b[1]) * canvas_size.y as f64);
                        item["w"] = json!(((a[0] - b[0]).abs() * canvas_size.x as f64).max(2.));
                        item["h"] = json!(((a[1] - b[1]).abs() * canvas_size.y as f64).max(2.));
                    }
                    doc.model["annotations"].as_array_mut().unwrap().push(item);
                    doc.revision += 1;
                    self.annotation = Some(id);
                    self.tab = "Objects".into();
                    self.changed();
                }
                self.points.clear();
            }
        }
        ui.add_space(6.);
        let mut preview_changed=false;
        ui.horizontal_wrapped(|ui| {
            if ui
                .button(if self.playback.is_some() {
                    "Pause"
                } else {
                    "Play"
                })
                .clicked()
            {
                self.play(&ctx);
            }
            if let Some(doc) = &mut self.document {
                let span = model::duration(&doc.model);
                if ui
                    .add(
                        egui::Slider::new(&mut doc.position, 0.0..=span.max(0.01))
                            .show_value(false),
                    )
                    .changed()
                {
                    self.playback = None;
                    self.frame_due = Some(Instant::now() + Duration::from_millis(25));
                }
                ui.label(format!("{} / {}", timecode(doc.position), timecode(span)));
            }
            preview_changed=crate::preview_toolbar::show(ui,&mut self.preview_settings,[canvas_size.x as u32,canvas_size.y as u32],source_fps);
            if self.playback.is_some()&&self.playback_fps>0.{ui.label(RichText::new(format!("Playing {:.0} FPS",self.playback_fps)).small()).on_hover_text("Frames delivered to this window over the last half-second. The display refresh rate and rendering load can limit visible playback.");}
        });
        if preview_changed{
            self.settings_revision+=1;
            let prefs=self.preview_settings;let _=prefs.write_config(&mut self.cfg);
            if self.preview_writer.send(prefs).is_err(){self.error="Could not save preview preferences. Restart the editor and try again.".into();}
            self.playback=None;self.playback_base=None;self.fast_key=None;self.frame_revision=u64::MAX;self.frame_due=Some(Instant::now());
        }
        ui.horizontal_wrapped(|ui| {
            ui.selectable_value(&mut self.tool,"Select".into(),"Select");
            let draw_label=if ["Pen","Line","Arrow","Rectangle"].contains(&self.tool.as_str()){self.tool.clone()}else{"Draw".into()};
            ui.menu_button(draw_label,|ui|{
                for tool in ["Pen","Line","Arrow","Rectangle"]{if ui.selectable_value(&mut self.tool,tool.into(),tool).clicked(){ui.close();}}
            });
            let privacy_label=if ["Blackout","Blur","Pixelate"].contains(&self.tool.as_str()){self.tool.clone()}else{"Privacy".into()};
            ui.menu_button(privacy_label,|ui|{
                for tool in ["Blackout","Blur","Pixelate"]{if ui.selectable_value(&mut self.tool,tool.into(),tool).clicked(){ui.close();}}
            });
            if ["Pen","Line","Arrow","Rectangle"].contains(&self.tool.as_str()){
                ui.color_edit_button_srgb(&mut self.ink);
                ui.add(egui::DragValue::new(&mut self.ink_width).speed(0.001).range(0.001..=0.1).prefix("Stroke "));
            }
        });
    }
    fn timeline(&mut self, ctx: &egui::Context) {
        if self.document.is_none() {
            return;
        }
        egui::TopBottomPanel::bottom("timeline")
            .resizable(true)
            .default_height(185.)
            .min_height(140.)
            .show(ctx, |ui| {
                ui.add_space(7.);
                ui.horizontal(|ui| {
                    ui.label(RichText::new("Timeline").strong());
                    ui.checkbox(&mut self.timeline_snap,"Snap").on_hover_text("Snap moved clips to frames, clip edges and the playhead");
                    ui.add(egui::DragValue::new(&mut self.timeline_zoom).range(1. ..=16.).speed(0.1).prefix("Zoom ").suffix("×"));
                    if ui.small_button("Fit").clicked(){self.timeline_zoom=1.;}
                });
                let Some(doc) = &mut self.document else {
                    return;
                };
                let total = model::duration(&doc.model).max(1.);
                let label_width = 160.;
                let width = (ui.available_width() - label_width - 8.).max(200.)*self.timeline_zoom;
                let px = width / total as f32;
                let mut changed = false;
                let mut selected = None;
                let mut drag = None;
                let mut track_changes=vec![];let mut begin_drag=false;
                egui::ScrollArea::both().show(ui, |ui| {
                    let (ruler,r)=ui.allocate_exact_size(Vec2::new(label_width+width+8.,24.),Sense::click_and_drag());
                    let ticks=(6.*self.timeline_zoom).ceil() as usize;
                    for tick in 0..=ticks{let x=ruler.left()+label_width+width*tick as f32/ticks as f32;ui.painter().text(Pos2::new(x,ruler.center().y),if tick==ticks{egui::Align2::RIGHT_CENTER}else{egui::Align2::LEFT_CENTER},timecode(total*tick as f64/ticks as f64),egui::FontId::proportional(11.),Color32::from_gray(174));}
                    if (r.clicked()||r.dragged())&&let Some(point)=r.interact_pointer_pos(){doc.position=((point.x-ruler.left()-label_width)/px).max(0.).min(total as f32) as f64;self.playback=None;self.frame_due=Some(Instant::now());}
                    for track in doc.model["tracks"].as_array().into_iter().flatten() {
                        let (rect, _) = ui.allocate_exact_size(
                            Vec2::new(label_width+width+8., 42.),
                            Sense::hover(),
                        );
                        let video = track["kind"] == "video";
                        let id = track["id"].as_str().unwrap_or("");
                        let mut enabled = if video {
                            track["visible"] != false
                        } else {
                            track["mute"] != true
                        };
                        let check = Rect::from_min_size(rect.min, Vec2::new(label_width - 8., 40.));
                        ui.scope_builder(egui::UiBuilder::new().max_rect(check), |ui| {
                            if ui
                                .checkbox(&mut enabled, track["name"].as_str().unwrap_or("Source"))
                                .changed()
                            {
                                track_changes.push((id.to_string(),video,enabled));
                            }
                        });
                        let bar = Rect::from_min_size(
                            Pos2::new(rect.left() + label_width, rect.top() + 5.),
                            Vec2::new(width, 32.),
                        );
                        ui.painter()
                            .rect_filled(bar, 3., Color32::from_rgb(32, 37, 40));
                        for clip in track["clips"].as_array().into_iter().flatten() {
                            let cid = clip["id"].as_str().unwrap_or("");
                            let left = bar.left() + number(clip, "start", 0.) as f32 * px;
                            let clip_rect = Rect::from_min_size(
                                Pos2::new(left, bar.top()),
                                Vec2::new((model::clip_duration(clip) as f32 * px).max(8.), 32.),
                            );
                            if !clip_rect.intersects(ui.clip_rect()){continue;}
                            let is_selected = doc.selected.contains(cid);
                            let color = if video {
                                Color32::from_rgb(59, 91, 108)
                            } else {
                                Color32::from_rgb(61, 103, 87)
                            };
                            ui.painter().rect_filled(clip_rect, 4., color);
                            if !video&&let Some((seconds,peaks))=self.waveforms.get(clip["source_id"].as_str().unwrap_or("")){
                                let first=(ui.clip_rect().left()-clip_rect.left()).max(2.)as usize;
                                let last=(ui.clip_rect().right()-clip_rect.left()).min(clip_rect.width()).max(4.)as usize;
                                for pixel in (first..last).step_by(3){
                                    let local=pixel as f64/px as f64;let source_time=model::source_offset(clip,local);
                                    let index=(source_time/seconds*peaks.len() as f64).max(0.) as usize;
                                    let amplitude=peaks.get(index).copied().unwrap_or(0.);let height=(amplitude*12.).clamp(0.5,12.);let x=clip_rect.left()+pixel as f32;
                                    ui.painter().line_segment([Pos2::new(x,clip_rect.center().y-height),Pos2::new(x,clip_rect.center().y+height)],Stroke::new(1_f32,Color32::from_rgba_unmultiplied(178,222,194,125)));
                                }
                            }
                            if is_selected {
                                ui.painter().rect_stroke(
                                    clip_rect,
                                    4.,
                                    Stroke::new(2_f32, Color32::from_rgb(194, 221, 214)),
                                    egui::StrokeKind::Inside,
                                );
                            }
                            ui.painter().text(
                                clip_rect.left_center() + Vec2::new(7., 0.),
                                egui::Align2::LEFT_CENTER,
                                clip["name"].as_str().unwrap_or("Clip"),
                                egui::FontId::proportional(12.),
                                Color32::WHITE,
                            );
                            let r =
                                ui.interact(clip_rect, ui.id().with(cid), Sense::click_and_drag());
                            if r.double_clicked(){self.properties_open=true;}
                            if r.clicked() {
                                selected = Some((cid.to_string(), ui.input(|i| i.modifiers.ctrl)));
                            }
                            if r.drag_started() {
                                begin_drag=true;
                                selected = Some((cid.to_string(), false));
                                self.drag_origin =
                                    Some(vec![(cid.into(), number(clip, "start", 0.))]);
                            }
                            if r.dragged() {
                                if let Some(origin) = &self.drag_origin {
                                    drag = Some((
                                        cid.to_string(),
                                        (origin[0].1 + r.drag_delta().x as f64 / px as f64).max(0.),
                                    ));
                                }
                            }
                            if r.drag_stopped() {
                                self.drag_origin = None;
                            }
                        }
                        let x = bar.left() + doc.position as f32 * px;
                        ui.painter().line_segment(
                            [
                                Pos2::new(x, bar.top() - 4.),
                                Pos2::new(x, bar.bottom() + 4.),
                            ],
                            Stroke::new(1.5_f32, Color32::from_rgb(237, 226, 183)),
                        );
                    }
                });
                if begin_drag||!track_changes.is_empty(){doc.checkpoint();}
                for(id,video,enabled)in track_changes{
                    if let Some(track)=doc.model["tracks"].as_array_mut().and_then(|xs|xs.iter_mut().find(|t|t["id"]==id)){
                        track[if video{"visible"}else{"mute"}]=json!(if video{enabled}else{!enabled});changed=true;
                    }
                }
                if let Some((id, multi)) = selected {
                    if !multi {
                        doc.selected.clear();
                    }
                    doc.selected.insert(id.clone());
                    doc.primary = Some(id);
                    self.annotation = None;
                }
                if let Some((id, mut start)) = drag {
                    if self.timeline_snap{
                        let fps=number(&doc.model,"fps",60.);start=(start*fps).round()/fps;
                        let mut edges=vec![doc.position];
                        for track in doc.model["tracks"].as_array().into_iter().flatten(){for clip in track["clips"].as_array().into_iter().flatten(){if clip["id"]!=id{let a=number(clip,"start",0.);edges.extend([a,a+model::clip_duration(clip)]);}}}
                        if let Some(nearest)=edges.into_iter().filter(|edge|(edge-start).abs()<8./px as f64).min_by(|a,b|(a-start).abs().total_cmp(&(b-start).abs())){start=nearest;}
                    }
                    for track in doc.model["tracks"].as_array_mut().unwrap() {
                        for clip in track["clips"].as_array_mut().unwrap() {
                            if clip["id"] == id {
                                clip["start"] = json!(start);
                            }
                        }
                    }
                    changed = true;
                }
                if changed {
                    doc.revision += 1;
                    self.changed();
                }
            });
    }
    fn inspector(&mut self, ctx: &egui::Context) {
        if !self.properties_open || self.document.is_none() {
            return;
        }
        let mut action = crate::inspector::Action::None;
        let old_revision = self.document.as_ref().unwrap().revision;
        egui::SidePanel::right("inspector")
            .default_width(if ctx.viewport_rect().width()<1100. {245.}else{280.})
            .min_width(245.)
            .max_width(480.)
            .resizable(true)
            .show(ctx, |ui| {
                ui.add_space(14.);
                    if let Some(doc) = &mut self.document {
                        action =
                            crate::inspector::show(ui, doc, &mut self.tab, &mut self.annotation);
                    }
            });
        if self.document.as_ref().unwrap().revision != old_revision {
            self.changed();
        }
        match action {
            crate::inspector::Action::None => (),
            crate::inspector::Action::Stabilize(clip) => {
                self.media_job("stabilize", json!({"clip":clip}))
            }
            crate::inspector::Action::Track(annotation) => {
                self.media_job("track", json!({"annotation":annotation}))
            }
            crate::inspector::Action::Transcribe(language) => {
                self.media_job("transcribe", json!({"language":language}))
            }
            crate::inspector::Action::ImportSrt => self.choose("import_srt", false),
            crate::inspector::Action::ExportSrt => self.choose("export_srt", true),
            crate::inspector::Action::Proxies => self.media_job("proxies", json!({})),
            crate::inspector::Action::SaveFrame => self.choose("frame", true),
            crate::inspector::Action::Lut => self.choose("lut", false),
            crate::inspector::Action::AddText => {
                if let Some(doc) = &mut self.document {
                    doc.checkpoint();
                    let id = model::uid();
                    let end=(doc.position+3.).min(model::duration(&doc.model)).max(doc.position+0.1);
                    doc.model["annotations"].as_array_mut().unwrap().push(json!({"id":id,"kind":"text","text":"Title","font":"Noto Sans","font_size":48,"color":"#ffffff","x":80,"y":80,"w":600,"h":100,"start":doc.position,"end":end,"opacity":1,"keys":{}}));
                    doc.revision += 1;
                    self.annotation = Some(id);
                    self.changed();
                }
            }
        }
    }
    fn dialogs(&mut self, ctx: &egui::Context) {
        if self.settings_open {
            let mut open = true;
            let mut gsr_action = None;
            egui::Window::new("Preferences")
                .open(&mut open)
                .default_width(560.)
                .resizable(false)
                .show(ctx, |ui| {
                    ui.label(
                        "GPU Screen Recorder owns capture: formats, replay durations, streaming, screenshots and shared shortcuts. Layer archives and edits the saved recordings.",
                    );
                    ui.add_space(10.);
                    ui.horizontal(|ui| {
                        if button(ui, "Open GSR", false)
                            .on_hover_text("Show or hide the GPU Screen Recorder overlay.")
                            .clicked()
                        {
                            gsr_action = Some(GsrAction::OpenGsr);
                        }
                        if button(ui, "GSR settings", true)
                            .on_hover_text("Open GPU Screen Recorder's recording settings.")
                            .clicked()
                        {
                            gsr_action = Some(GsrAction::GsrSettings);
                        }
                    });
                    ui.add_space(14.);
                    egui::CollapsingHeader::new("Advanced GSR capture options").default_open(std::env::args().any(|a|a=="--qa-advanced")).show(ui,|ui|{
                        egui::ScrollArea::vertical().id_salt("advanced-gsr-scroll").max_height(360.).show(ui,|ui|{
                            if crate::advanced_capture::show(ui,&mut self.advanced_draft){self.advanced_saved=false;}
                        });
                        ui.label("Saved options apply when the next capture session starts. The current replay buffer stays running.");
                        if ui.add_enabled(!self.jobs.busy(),egui::Button::new("Save capture options")).clicked(){self.jobs.start(&self.root,"capture_settings",json!({"capture_advanced":self.advanced_draft}));}
                        if self.advanced_saved{ui.label("Capture options saved for the next session.");}
                    });
                    ui.separator();
                    ui.label(RichText::new("Recording library").strong());
                    ui.add(
                        egui::TextEdit::singleline(&mut self.library_edit)
                            .desired_width(f32::INFINITY),
                    );
                    if ui.button("Save library location").clicked() {
                        let library = self.library_edit.clone();
                        let tx = self.tx.clone();
                        thread::spawn(move || {
                            let result = ipc::save_library(&library).map_err(|e| e.to_string());
                            let _ = tx.send(Message::LibrarySaved(library, result));
                        });
                    }
                    ui.add_space(8.);
                    gsr_controls::hint(ui);
                    ui.label(
                        RichText::new(
                            "Displays and application sounds are kept as independent sources. Choose what remains while editing.",
                        )
                        .small()
                        .color(Color32::from_gray(185)),
                    );
                });
            self.settings_open = open;
            if let Some(chosen) = gsr_action {
                self.gsr(chosen);
            }
        }
        if self.export_open {
            let mut open = true;
            let mut choose = false;
            egui::Window::new("Export recording")
                .open(&mut open)
                .default_width(480.)
                .resizable(false)
                .show(ctx, |ui| {
                    let eligibility=self.document.as_ref().map(|doc|crate::fast_export::plan(&doc.project,&doc.model)).unwrap_or_else(||Err(anyhow::anyhow!("Open a recording first.")));
                    let can_copy=!self.batch_mode&&!self.export_audio&&eligibility.is_ok();
                    ui.add_enabled(can_copy,egui::Checkbox::new(&mut self.export_fast,"Quick export · copy original video"));
                    if can_copy&&self.export_fast{ui.label("Copies video without re-encoding. Selected audio is mixed separately.");}
                    else if !self.batch_mode&&let Err(error)=&eligibility{ui.label(egui::RichText::new(format!("Render required: {error}")).small());}
                    let render=!(can_copy&&self.export_fast);
                    ui.add_space(8.);
                    ui.add_enabled_ui(render,|ui|{egui::Grid::new("export-options")
                        .spacing(Vec2::new(20., 12.))
                        .show(ui, |ui| {
                            ui.label("Format / encoder");
                            egui::ComboBox::from_id_salt("encoder")
                                .selected_text(&self.export_codec)
                                .show_ui(ui, |ui| {
                                    for (label, value) in [
                                        ("H.264 · CPU", "libx264"),
                                        ("H.265 · CPU", "libx265"),
                                        ("H.264 · NVIDIA", "h264_nvenc"),
                                        ("H.265 · NVIDIA", "hevc_nvenc"),
                                        ("AV1 · NVIDIA", "av1_nvenc"),
                                        ("H.264 · VA-API", "h264_vaapi"),
                                        ("H.265 · VA-API", "hevc_vaapi"),
                                        ("AV1 · VA-API", "av1_vaapi"),
                                        ("AV1 · CPU", "libaom-av1"),
                                    ] {
                                        ui.selectable_value(
                                            &mut self.export_codec,
                                            value.into(),
                                            label,
                                        );
                                    }
                                });
                            ui.end_row();
                            ui.label("Quality");
                            ui.add(egui::Slider::new(&mut self.export_quality, 0..=51));
                            ui.end_row();
                            ui.label("Size");
                            ui.horizontal(|ui| {
                                ui.add(
                                    egui::DragValue::new(&mut self.export_size[0]).range(2..=16384),
                                );
                                ui.label("×");
                                ui.add(
                                    egui::DragValue::new(&mut self.export_size[1]).range(2..=16384),
                                );
                            });
                            ui.end_row();
                            ui.label("Frame rate");
                            ui.add(
                                egui::DragValue::new(&mut self.export_fps)
                                    .range(1..=240)
                                    .suffix(" fps"),
                            );
                            ui.end_row();
                        });
                    ui.add_enabled_ui(!self.export_codec.ends_with("_vaapi"),|ui|{ui.horizontal(|ui|{ui.label("Encoding speed");egui::ComboBox::from_id_salt("export-speed").selected_text(match self.export_preset.as_str(){"ultrafast"=>"Fastest","slow"=>"Better compression",_=>"Balanced"}).show_ui(ui,|ui|{for(label,value)in[("Fastest","ultrafast"),("Balanced","fast"),("Better compression","slow")]{ui.selectable_value(&mut self.export_preset,value.into(),label);}});});});});
                    ui.checkbox(&mut self.export_audio, "Audio only");
                    ui.add_space(12.);
                    if button(ui, "Choose destination…", true).clicked() {
                        choose = true;
                    }
                    if self.batch_mode{ui.label(format!("{} selected recordings · existing exports are skipped",self.batch_selected.len()));}
                    ui.label(
                        RichText::new("Original recording files are preserved.")
                            .small()
                            .color(Color32::from_gray(180)),
                    );
                });
            self.export_open = open;
            if choose {
                self.choose(if self.batch_mode{"batch_export"}else{"export"}, true);
            }
        }
    }
    fn shortcuts(&mut self, ctx: &egui::Context) {
        if self.voiceover_open || ctx.wants_keyboard_input() {
            return;
        }
        let mut changed = false;
        let mut play = false;
        let mut export = false;
        ctx.input_mut(|i| {
            let ctrl = egui::Modifiers::CTRL;
            if i.consume_key(egui::Modifiers::NONE, egui::Key::Space) {
                play = true;
            }
            if i.consume_key(ctrl, egui::Key::E) {
                export = true;
            }
            if let Some(doc) = &mut self.document {
                if i.consume_key(ctrl, egui::Key::Z) {
                    doc.undo();
                    changed = true;
                }
                if i.consume_key(egui::Modifiers::CTRL | egui::Modifiers::SHIFT, egui::Key::Z) {
                    doc.redo();
                    changed = true;
                }
                if i.consume_key(ctrl, egui::Key::B) {
                    if let Err(e) = doc.split_selected() {
                        self.error = e.to_string();
                    }
                    changed = true;
                }
                if i.consume_key(egui::Modifiers::NONE, egui::Key::Delete) {
                    doc.delete_selected();
                    changed = true;
                }
                if i.consume_key(ctrl, egui::Key::C) {
                    doc.copy_selected();
                }
                if i.consume_key(ctrl, egui::Key::V) {
                    doc.paste();
                    changed = true;
                }
                if i.consume_key(ctrl, egui::Key::D) {
                    doc.duplicate_selected();
                    changed = true;
                }
                for (key, delta) in [(egui::Key::ArrowLeft, -1.), (egui::Key::ArrowRight, 1.)] {
                    if i.consume_key(egui::Modifiers::NONE, key) {
                        doc.position = (doc.position + delta / number(&doc.model, "fps", 60.))
                            .clamp(0., model::duration(&doc.model));
                        self.playback = None;
                        self.frame_due = Some(Instant::now());
                    }
                }
            }
        });
        if changed {
            self.changed();
        }
        if play {
            self.play(&ctx);
        }
        if export && self.document.is_some() && !self.jobs.busy() {
            self.batch_mode = false;
            self.export_open = true;
        }
    }
}
impl eframe::App for Studio {
    fn on_exit(&mut self,gl:Option<&eframe::glow::Context>){if let (Some(gpu),Some(gl))=(&self.gpu,gl){gpu.destroy(gl);}}
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        let frame_started=Instant::now();
        self.passes += 1;
        if self.close_started.is_none() { self.events(ctx); }
        self.save_events(ctx);
        if let Some(seconds)=self.qa_play_seconds{
            if self.qa_play_started.is_none()&&!self.opening&&self.document.is_some()&&self.startup.elapsed().as_secs_f64()>=0.7{self.play(ctx);self.qa_play_started=Some(Instant::now());}
            if self.qa_play_started.is_some_and(|t|t.elapsed().as_secs_f64()>=seconds){
                if let Some(doc)=&self.document{let canvas=[number(&doc.model,"width",1920.)as u32,number(&doc.model,"height",1080.)as u32];println!("LAYER_PLAYBACK_QA {}",json!({"target_fps":self.preview_settings.fps(&doc.model,&doc.project),"delivered_fps":self.playback_fps,"received_frames":self.playback_sequence,"size":self.preview_settings.size(canvas),"source_position":doc.position,"wall_after_first_frame":self.playback_first_frame.map(|t|t.elapsed().as_secs_f64()),"error":self.error}));}
                self.qa_exit=true;ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            ctx.request_repaint_after(Duration::from_millis(4));
        }
        if self.qa_overlay_steps<self.qa_overlay_target&&self.startup.elapsed().as_secs_f64()>=self.qa_after{
            let source_ready=self.fast_scene.as_ref().is_some_and(|scene|self.fast_cache.ready(scene)&&crate::fast_preview::text_keys(scene).iter().all(|key|self.overlays.ready(key))&&crate::fast_preview::caption_keys(scene).iter().all(|key|self.overlays.caption_ready(key)))&&(!self.underlay||self.underlay_cached==self.underlay_key());
            if source_ready&&let Some(doc)=&mut self.document{
                if let Some(ann)=doc.model["annotations"].as_array_mut().and_then(|annotations|annotations.iter_mut().find(|ann|["redact","blur","pixelate"].contains(&ann["kind"].as_str().unwrap_or("")))){
                    let x=number(ann,"x",0.);ann["x"]=json!(x+if self.qa_overlay_steps%2==0{1.}else{-1.});doc.revision+=1;
                }
                self.qa_overlay_steps+=1;self.changed();ctx.request_repaint();
            }
        }
        if let Some(gpu)=&self.gpu{
            if let Some(result)=gpu.take_capture(){
                match result{Ok(path)=>{let stats=gpu.statistics.lock().unwrap();println!("LAYER_GPU_CAPTURE_OK {} frames={} roi_pixels={} cpu_ms={:.3} source_decodes={} base_renders={} underlay_renders={} overlay_edits={} overlay_rasters={} held_frames={}",path.display(),stats.frames,stats.effect_pixels,stats.last_cpu_ms,self.fast_cache.decoded,stats.base_renders,self.underlay_generation,self.qa_overlay_steps,self.overlays.rasterized,stats.held_frames);},Err(error)=>eprintln!("GPU capture failed: {error}")}
                if let Some(position)=self.qa_positions.pop_front(){if let Some(doc)=&mut self.document{doc.position=position.min(model::duration(&doc.model));}self.fast_key=None;self.frame_due=Some(Instant::now());self.qa_preview_requested=false;self.qa_capture_index+=1;ctx.request_repaint();}
                else{ctx.send_viewport_cmd(egui::ViewportCommand::Close);self.qa_exit=true;}
            }
            if self.startup.elapsed().as_secs_f64()>=self.qa_after&&self.qa_overlay_steps>=self.qa_overlay_target&&self.document.as_ref().is_some_and(|doc|self.fast_key==Some((doc.revision,doc.position.to_bits())))&&!self.qa_preview_requested&&let Some(path)=self.qa_preview.clone(){let path=if self.qa_sequence{path.with_file_name(format!("{}-{}.png",path.file_stem().unwrap_or_default().to_string_lossy(),self.qa_capture_index))}else{path};gpu.request_capture(path,ctx.clone());self.qa_preview_requested=true;ctx.request_repaint();}
        }

        if self.allow_close {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            return;
        }
        if self.closing(ctx) { return; }
        if self.startup.elapsed().as_secs_f64()>self.qa_after*0.4 && !self.opening {
            if let Some(panel)=self.qa_panel.take(){match panel.as_str(){
                "settings"=>self.settings_open=true,
                "export"=>self.export_open=self.document.is_some(),
                "voiceover"=>self.open_voiceover(),
                _=>()
            }}
        }
        self.shortcuts(ctx);
        self.toolbar(ctx);
        self.archive(ctx);
        if !self.error.is_empty() {
            egui::TopBottomPanel::top("errors").show(ctx, |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.colored_label(Color32::from_rgb(255, 185, 174), &self.error);
                    if ui.small_button("Dismiss").clicked() {
                        self.error.clear();
                    }
                });
            });
        }
        if self.status["running"] == true {
            if let Some(warnings) = self.status["warnings"].as_array().filter(|items| !items.is_empty()) {
                egui::TopBottomPanel::top("audio-compatibility").show(ctx, |ui| {
                    egui::CollapsingHeader::new("Audio capture compatibility").default_open(true).show(ui, |ui| {
                        for warning in warnings.iter().filter_map(Value::as_str) {
                            ui.colored_label(Color32::from_rgb(228, 201, 141), warning);
                        }
                    });
                });
            }
        }
        if self.jobs.busy() {
            egui::TopBottomPanel::bottom("progress").show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.add(
                        egui::ProgressBar::new(self.progress as f32)
                            .desired_width(300.)
                            .show_percentage(),
                    );
                    ui.label(if self.jobs.running.iter().any(|(_,op,_)|op=="capture_settings"){"Saving capture options…"}else{"Processing media…"});
                    if ui.button("Cancel").clicked() {
                        for (id, _, _) in &self.jobs.running {
                            self.jobs.cancel(*id);
                        }
                    }
                });
            });
        }
        self.timeline(ctx);
        self.inspector(ctx);
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.add_space(12.);
            if self.document.is_some() {
                self.canvas(ui);
            } else {
                ui.vertical_centered(|ui| {
                    ui.add_space(ui.available_height() * 0.3);
                    ui.heading(if self.opening { "Opening recording…" } else { "Your recordings, ready to edit" });
                    ui.add_space(10.);
                    ui.label("Capture with GPU Screen Recorder. Edit and archive in Layer.");
                    ui.label("Select a recording from the library, or use Capture to save a replay.");
                    ui.add_space(6.);
                    gsr_controls::hint(ui);
                    ui.add_space(16.);
                    if button(ui, "Open recording…", true).clicked() {
                        self.choose("open", false);
                    }
                });
            }
        });
        self.dialogs(ctx);
        self.voiceover_dialog(ctx);
        if self.screenshot.is_some()
            && self.startup.elapsed().as_secs_f64() > self.qa_after
            && !self.qa_requested
        {
            if self.qa_actions {
                if let Some(doc) = &mut self.document {
                    if let Some(clip) = doc.model["tracks"]
                        .as_array()
                        .and_then(|a| a.first())
                        .and_then(|t| t["clips"].as_array())
                        .and_then(|a| a.first())
                        .cloned()
                    {
                        if let Some(id) = clip["id"].as_str() {
                            doc.selected = HashSet::from([id.to_string()]);
                            doc.primary = Some(id.into());
                            doc.position = (model::clip_duration(&clip) * 0.5).max(0.1);
                            let _ = doc.split_selected();
                            doc.undo();
                            self.saver.submit(doc.path.clone(), doc.project.clone(), doc.model.clone(), doc.revision);
                        }
                    }
                }
            }
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            self.qa_requested = true;
        }
        let screenshots: Vec<_> = ctx.input(|i| {
            i.events
                .iter()
                .filter_map(|e| {
                    if let egui::Event::Screenshot { image, .. } = e {
                        Some(image.clone())
                    } else {
                        None
                    }
                })
                .collect()
        });
        for image in screenshots {
            if let Some(path) = self.screenshot.take() {
                let tx = self.tx.clone();
                thread::spawn(move || {
                let result = (|| -> Result<PathBuf> {
                let bytes: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)?;
                }
                image::save_buffer(
                    &path,
                    &bytes,
                    image.width() as u32,
                    image.height() as u32,
                    image::ColorType::Rgba8,
                )?;
                Ok(path)
                })();
                let _ = tx.send(Message::ScreenshotSaved(result.map_err(|e|e.to_string())));
                });
            }
        }
        if self.qa_requested && !self.qa_exit && self.startup.elapsed().as_secs_f64() > self.qa_after + 8. {
            eprintln!("LAYER_UI_QA_ERROR screenshot deadline exceeded");
            self.qa_exit = true;
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        if self.passes<=3 || frame_started.elapsed()>Duration::from_millis(250){eprintln!("LAYER_UI_FRAME {} {}ms",self.passes,frame_started.elapsed().as_millis());}
        ctx.request_repaint_after(Duration::from_millis(100));
    }
}
pub fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--version") {
        println!("layer-studio {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let value = |flag: &str| {
        args.iter()
            .position(|a| a == flag)
            .and_then(|i| args.get(i + 1))
            .cloned()
    };
    let root = value("--root").map(PathBuf::from).unwrap_or(
        std::env::current_exe()?
            .parent()
            .and_then(Path::parent)
            .context("Application root")?
            .to_path_buf(),
    );
    let open = value("--project").map(PathBuf::from);
    let Some(instance) = ipc::instance(open.clone())? else {
        return Ok(());
    };
    let screenshot = value("--qa-screenshot").map(PathBuf::from);
    let after = value("--qa-after")
        .and_then(|s| s.parse().ok())
        .unwrap_or(3.);
    let size = value("--qa-size")
        .and_then(|s| {
            s.split_once('x')
                .and_then(|(a, b)| Some((a.parse::<f32>().ok()?, b.parse::<f32>().ok()?)))
        })
        .unwrap_or((1440., 900.));
    let qa_actions = args.iter().any(|a| a == "--qa-actions");
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([size.0, size.1])
            .with_min_inner_size([800., 600.])
            .with_app_id(std::env::var("FLATPAK_ID").unwrap_or_else(|_|"layer-recorder".into())),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eprintln!("LAYER_UI_BOOT creating_window backend={} display={} wayland={}",
        std::env::var("WINIT_UNIX_BACKEND").unwrap_or_else(|_| "automatic".into()),
        std::env::var_os("DISPLAY").is_some(), std::env::var_os("WAYLAND_DISPLAY").is_some());
    eframe::run_native(
        "Layer",
        options,
        Box::new(move |cc| {
            Ok(Box::new(Studio::new(
                root, instance, open, screenshot, after, qa_actions, cc,
            )))
        }),
    )
    .map_err(|e| anyhow::anyhow!("Native window: {e}"))?;
    Ok(())
}
