# Native editor boundary

The public UI is one Rust native window (eframe 0.33.3, glow, X11 + Wayland). Archive selection loads the timeline in that same window; no second editor opens. All UI and documentation are English. Fixed neutral native palette, Noto Sans if available, no personal system-theme integration in public code. Preserve version-1 projects and all studio JSON fields/advanced edit decisions. Existing Python FFmpeg render/analysis functions stay headless and are loaded only for jobs; Python/Qt must not be loaded to open the Rust window.

## Document API (model.rs)

Use serde_json::Value to retain unknown fields. Export `duration(model)`, `clip_duration(clip)`, `source_offset(clip,t)`, `value_at(item,key,t,default)`, `from_project(project)`, `uid()`. Return f64 for times/numbers, Result<Value> for from_project.

`pub struct Document { pub path: PathBuf, pub project: Value, pub model: Value, pub selected: HashSet<String>, pub primary: Option<String>, pub position: f64, pub revision:u64, pub clipboard: Vec<Value> }` with private history fields.

Methods: `load(path: &Path)->Result<Self>`, `save(&mut self)->Result<()>`, `checkpoint(&mut self)`, `undo(&mut self)`, `redo(&mut self)`, `selected_clip(&self)->Option<&Value>`, `selected_clip_mut(&mut self)->Option<&mut Value>`, `split_selected(&mut self)->Result<()>`, `delete_selected(&mut self)`, `duplicate_selected(&mut self)`, `copy_selected(&mut self)`, `paste(&mut self)`, `move_selected(&mut self,delta:f64)`, `trim_selected(&mut self,left:bool,delta:f64)->Result<()>`.

Mutating editing methods checkpoint once internally, update revision, validate bounds and preserve source media. Direct UI edits use checkpoint before editing. Save changes only project.studio atomically, retaining original sources/metadata and unknown project fields. Relative media paths must stay inside the project folder. Loading supports directory/project.katman. Baseline algorithms are in katman/studio_model.py; preserve variable speed, freeze, reverse, timing/keyframe/fade envelope slicing. Tests include parity fixtures against Python model where practical.

## Headless worker API (editor_worker.py)

One JSON object on stdin: {operation, path, model?, output?, position?, source?, overlay?, options?, ...}. One JSON result line on stdout: {ok:true,data:...} or {ok:false,error:...}. Worker never opens GUI. stderr is job log. Progress lines may be {event:"progress",value:float}; final is above. Source operations: frame (returns {file}), preview (returns {file}), export ({file}), import (returns {project,model}), proxies, stabilize, track, transcribe. Existing renderer functions preserve all advanced edits.

## Inspector module (inspector.rs)

`pub enum Action { None, Stabilize(Value), Track(Value), Transcribe(String), ImportSrt, ExportSrt, Proxies, AddText, SaveFrame, Lut }` and `pub fn show(ui:&mut eframe::egui::Ui, doc:&mut Document, tab:&mut String, selected_annotation:&mut Option<String>)->Action`.

Tabs Clip / Video / Audio / Objects / Captions / Canvas; selected clip properties and time/timing numeric alternatives to timeline dragging. Preserve annotation/subtitle styles and unknown fields. Native controls, no raw JSON user editor. Keyframe tables and speed-segment tables, opacity, transforms, normalized crop, color, LUT button Action::Lut, fades, gain/denoise/normalize, reverse/freeze, borders/blackout; object time/size/text/color, annotation selection and privacy Track action; subtitle rows add/remove/edit/import/export/offline Transcribe; canvas dimensions/fps/background. Work on model JSON, checkpoint BEFORE changes (snapshot old model; after egui mutation swap snapshot into model, checkpoint, restore edited model), update revision. Do not save media or invoke subprocesses. Parent handles actions and persistence.
