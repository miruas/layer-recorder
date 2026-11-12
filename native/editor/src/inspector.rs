// SPDX-License-Identifier: GPL-3.0-only
//! Native inspector forms for the selected clip, objects, captions and canvas.
//!
//! Widgets edit only their active JSON scope. Undo snapshots are made only after
//! a real edit; a continuous pointer gesture shares one checkpoint. Unrelated
//! tracks and extension fields are never copied during idle clip inspection.
//! This module never touches media, files or subprocesses.

use eframe::egui::{self, Ui};
use serde_json::{Map, Value, json};

use crate::model::{Document, clip_duration, duration, source_offset, uid, value_at};

/// Work for the parent window. The inspector itself only edits model JSON.
pub enum Action {
    None,
    Stabilize(Value),
    Track(Value),
    Transcribe(String),
    ImportSrt,
    ExportSrt,
    Proxies,
    AddText,
    SaveFrame,
    Lut,
}

#[derive(Clone)]
struct SelectedClip {
    id: String,
    kind: String,
    source: Value,
}

#[derive(Clone, Copy)]
struct KeyProperty {
    key: &'static str,
    label: &'static str,
    default: f64,
    min: f64,
    max: f64,
    decimals: usize,
}

const VIDEO_KEYS: &[KeyProperty] = &[
    KeyProperty {
        key: "x",
        label: "Left",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "y",
        label: "Top",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "w",
        label: "Width",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "h",
        label: "Height",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "rotation",
        label: "Rotation (deg)",
        default: 0.0,
        min: -360.0,
        max: 360.0,
        decimals: 1,
    },
    KeyProperty {
        key: "opacity",
        label: "Opacity",
        default: 1.0,
        min: 0.0,
        max: 1.0,
        decimals: 2,
    },
];

const AUDIO_KEYS: &[KeyProperty] = &[KeyProperty {
    key: "gain",
    label: "Level",
    default: 1.0,
    min: 0.0,
    max: 4.0,
    decimals: 2,
}];

const ANNOTATION_KEYS: &[KeyProperty] = &[
    KeyProperty {
        key: "x",
        label: "Left",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "y",
        label: "Top",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "w",
        label: "Width",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "h",
        label: "Height",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "opacity",
        label: "Opacity",
        default: 1.0,
        min: 0.0,
        max: 1.0,
        decimals: 2,
    },
];

const MASK_KEYS: &[KeyProperty] = &[
    KeyProperty {
        key: "x",
        label: "Left",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "y",
        label: "Top",
        default: 0.0,
        min: -16384.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "w",
        label: "Width",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
    KeyProperty {
        key: "h",
        label: "Height",
        default: 100.0,
        min: 2.0,
        max: 16384.0,
        decimals: 1,
    },
];

const LANGUAGES: [(&str, &str); 8] = [
    ("Auto-detect", "auto"),
    ("Turkish", "tr"),
    ("English", "en"),
    ("German", "de"),
    ("French", "fr"),
    ("Spanish", "es"),
    ("Japanese", "ja"),
    ("Chinese", "zh"),
];

/// Small reversible patches let checkpoint() see the pre-edit model without
/// copying unrelated tracks every frame. Missing root fields remain missing.
struct EditSnapshot(Vec<(String, Option<Value>)>);

impl EditSnapshot {
    fn capture(doc: &Document, tab: &str) -> Self {
        let mut snapshot = Self(Vec::new());
        match tab {
            "Objects" => snapshot.field(&doc.model, "annotations"),
            "Captions" => {
                snapshot.field(&doc.model, "subtitles");
                snapshot.field(&doc.model, "subtitle_style");
            }
            "Canvas" => {
                for key in ["width", "height", "fps", "background"] {
                    snapshot.field(&doc.model, key);
                }
            }
            _ => {
                if let Some(clip) = doc.selected_clip() {
                    let id = clip.get("id");
                    if let Some(tracks) = doc.model.get("tracks").and_then(Value::as_array) {
                        for (track_index, track) in tracks.iter().enumerate() {
                            if let Some(clips) = track.get("clips").and_then(Value::as_array) {
                                if let Some(clip_index) =
                                    clips.iter().position(|c| c.get("id") == id)
                                {
                                    snapshot.0.push((
                                        format!("/tracks/{track_index}/clips/{clip_index}"),
                                        Some(clip.clone()),
                                    ));
                                    break;
                                }
                            }
                        }
                    }
                }
            }
        }
        snapshot
    }

    fn field(&mut self, model: &Value, key: &str) {
        let path = format!("/{key}");
        if !self.0.iter().any(|(existing, _)| existing == &path) {
            self.0.push((path, model.get(key).cloned()));
        }
    }

    fn changed(&self, model: &Value) -> bool {
        self.0
            .iter()
            .any(|(path, before)| model.pointer(path) != before.as_ref())
    }

    /// Swap twice around checkpoint() to restore precisely the edited scopes.
    fn swap(&mut self, model: &mut Value) {
        for (path, value) in &mut self.0 {
            if path[1..].contains('/') {
                if let (Some(target), Some(value)) = (model.pointer_mut(path), value.as_mut()) {
                    std::mem::swap(target, value);
                }
            } else if let Some(object) = model.as_object_mut() {
                let key = &path[1..];
                let current = object.remove(key);
                if let Some(previous) = value.take() {
                    object.insert(key.to_owned(), previous);
                }
                *value = current;
            }
        }
    }

    fn commit(mut self, doc: &mut Document, checkpoint: bool) -> bool {
        if !self.changed(&doc.model) {
            return false;
        }
        if checkpoint {
            self.swap(&mut doc.model);
            doc.checkpoint();
            self.swap(&mut doc.model);
        }
        doc.revision = doc.revision.saturating_add(1);
        true
    }
}

#[derive(Clone)]
struct PointerEdit {
    scope: String,
    revision: u64,
}

pub fn show(
    ui: &mut eframe::egui::Ui,
    doc: &mut Document,
    tab: &mut String,
    selected_annotation: &mut Option<String>,
) -> Action {
    let mut action = Action::None;

    if !matches!(
        tab.as_str(),
        "Clip" | "Video" | "Audio" | "Objects" | "Captions" | "Canvas"
    ) {
        *tab = "Clip".to_owned();
    }

    ui.horizontal_wrapped(|ui| {
        for name in ["Clip", "Video", "Audio", "Objects", "Captions", "Canvas"] {
            ui.selectable_value(tab, name.to_owned(), name);
        }
    });
    ui.separator();

    let mut snapshot = EditSnapshot::capture(doc, tab);
    let gesture_id = egui::Id::new("inspector_pointer_edit");
    let scope = format!(
        "{}:{}:{:?}:{:?}",
        doc.path.display(),
        tab,
        doc.primary,
        selected_annotation
    );
    let previous = ui.data_mut(|data| data.get_temp::<PointerEdit>(gesture_id));
    let continue_gesture = previous
        .as_ref()
        .is_some_and(|edit| edit.scope == scope && edit.revision == doc.revision);

    egui::ScrollArea::vertical()
        .id_salt("inspector_scroll")
        .auto_shrink([false, false])
        .show(ui, |ui| {
            let chosen = match tab.as_str() {
                "Video" => video_tab(ui, doc),
                "Audio" => audio_tab(ui, doc),
                "Objects" => objects_tab(ui, doc, selected_annotation),
                "Captions" => captions_tab(ui, doc),
                "Canvas" => canvas_tab(ui, doc, &mut snapshot),
                _ => clip_tab(ui, doc),
            };
            if !matches!(chosen, Action::None) {
                action = chosen;
            }
        });

    let changed = snapshot.commit(doc, !continue_gesture);
    let pointer_down = ui.input(|input| input.pointer.any_down());
    ui.data_mut(|data| {
        if pointer_down && (changed || continue_gesture) {
            data.insert_temp(
                gesture_id,
                PointerEdit {
                    scope,
                    revision: doc.revision,
                },
            );
        } else {
            data.remove::<PointerEdit>(gesture_id);
        }
    });

    action
}

// ---------------------------------------------------------------------------
// Clip and timing
// ---------------------------------------------------------------------------

fn clip_tab(ui: &mut Ui, doc: &mut Document) -> Action {
    let action = Action::None;
    let Some(meta) = selected_clip_meta(doc) else {
        ui.label("Select a timeline clip to edit its properties.");
        return action;
    };
    let position = doc.position.max(0.0);
    let Some(clip) = doc.selected_clip_mut() else {
        ui.label("Select a timeline clip to edit its properties.");
        return action;
    };

    let start = num_at(clip, "start", 0.0);
    let local = (position - start).max(0.0);
    let video = meta.kind == "video";

    egui::Grid::new(("inspector_clip", meta.id.as_str()))
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            text_control(ui, clip, "name", "Clip name");

            ui.label("Source");
            let source_name = meta
                .source
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("Unknown source");
            ui.label(source_name);
            ui.end_row();

            ui.label("Kind");
            ui.label(if meta.kind.is_empty() {
                "Unknown".to_owned()
            } else {
                capitalize(&meta.kind)
            });
            ui.end_row();
        });

    ui.strong("Timing");
    let mut timing_changed = false;
    let mut speed_changed = false;
    let mut freeze_changed = false;

    egui::Grid::new(("inspector_clip_timing", meta.id.as_str()))
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            let start_changed = number_control(
                ui,
                clip,
                "start",
                "Start on timeline",
                0.0,
                0.0,
                100000.0,
                0.01,
                3,
                local,
            );
            let in_changed = number_control(
                ui,
                clip,
                "in",
                "Source in",
                0.0,
                0.0,
                100000.0,
                0.01,
                3,
                local,
            );
            let length_changed = number_control(
                ui,
                clip,
                "length",
                "Source length",
                1.0,
                0.01,
                100000.0,
                0.01,
                3,
                local,
            );
            timing_changed = start_changed || in_changed || length_changed;
            if in_changed || length_changed {
                if let Some(object) = clip.as_object_mut() {
                    object.remove("stabilization");
                }
            }
        });

    egui::CollapsingHeader::new("Speed and freeze frame")
        .default_open(false)
        .show(ui, |ui| {
            egui::Grid::new(("inspector_clip_speed", meta.id.as_str()))
                .num_columns(2)
                .spacing([8.0, 6.0])
                .show(ui, |ui| {
                    speed_changed = number_control(
                        ui,
                        clip,
                        "speed",
                        "Speed (x)",
                        1.0,
                        0.25,
                        4.0,
                        0.01,
                        2,
                        local,
                    );
                    let hold_changed = number_control(
                        ui,
                        clip,
                        "hold_duration",
                        "Freeze duration",
                        1.0,
                        0.04,
                        600.0,
                        0.01,
                        2,
                        local,
                    );

                    ui.label("Freeze frame");
                    let mut freeze = truthy(clip.get("freeze"));
                    let response = ui
                        .add_enabled(video, egui::Checkbox::without_text(&mut freeze))
                        .on_hover_text("Freeze frame");
                    if response.changed() {
                        let freeze_at = source_offset(clip, local);
                        set_bool(clip, "freeze", freeze);
                        if freeze {
                            set_num(clip, "freeze_at", freeze_at);
                        }
                        freeze_changed = true;
                    }
                    ui.end_row();

                    let reverse_changed = bool_control(ui, clip, "reverse", "Reverse playback");

                    timing_changed |=
                        speed_changed || hold_changed || freeze_changed || reverse_changed;
                });
            if speed_segments_table(ui, clip) {
                timing_changed = true;
                speed_changed = true;
            }
        });
    if !video {
        note(
            ui,
            "Freeze and hold duration are available for video clips. Audio timing still follows source in, length and speed.",
        );
    }

    if timing_changed {
        if truthy(clip.get("freeze")) {
            let hold = num_at(clip, "hold_duration", 1.0).max(0.04);
            set_num(clip, "hold_duration", hold);
            set_num(clip, "duration", hold);
        } else {
            set_num(clip, "duration", clip_duration(clip));
        }
    }

    if speed_changed || freeze_changed {
        let total = clip_duration(clip);
        set_num(clip, "effect_offset", 0.0);
        set_num(clip, "effect_length", total);
    }

    action
}

fn speed_segments_table(ui: &mut Ui, clip: &mut Value) -> bool {
    let mut changed = false;
    let length = num_at(clip, "length", 1.0).max(0.01);

    egui::CollapsingHeader::new("Variable speed segments")
        .default_open(false)
        .show(ui, |ui| {
            let mut rows = pairs(clip.get("speed_keys"));
            let synthetic = rows.is_empty();
            if synthetic {
                rows.push((0.0, num_at(clip, "speed", 1.0)));
            }

            egui::Grid::new("inspector_speed_rows")
                .num_columns(3)
                .striped(true)
                .show(ui, |ui| {
                    ui.strong("Source offset (s)");
                    ui.strong("Speed (x)");
                    ui.strong("");
                    ui.end_row();
                    let mut remove = None;
                    for (index, row) in rows.iter_mut().enumerate() {
                        let time_changed =
                            drag_f64(ui, &mut row.0, 0.0, length, 0.01, 3);
                        let rate_changed =
                            drag_f64(ui, &mut row.1, 0.25, 4.0, 0.01, 2);
                        if time_changed || rate_changed {
                            changed = true;
                        }
                        if ui.small_button("Remove").clicked() {
                            remove = Some(index);
                        }
                        ui.end_row();
                    }
                    if let Some(index) = remove {
                        rows.remove(index);
                        changed = true;
                    }
                });

            if ui.button("Add segment").clicked() {
                let next = rows
                    .iter()
                    .map(|(time, _)| *time)
                    .fold(0.0_f64, f64::max)
                    .min(length);
                rows.push((next, 1.0));
                changed = true;
            }

            note(
                ui,
                "Each rate starts at its source offset and stays active until the next segment. Rates are 0.25x to 4x.",
            );

            if changed {
                rows.sort_by(|left, right| {
                    left.0.partial_cmp(&right.0).unwrap_or(std::cmp::Ordering::Equal)
                });
                let values = rows
                    .iter()
                    .map(|(time, rate)| json!([*time, *rate]))
                    .collect::<Vec<_>>();
                set_value(clip, "speed_keys", Value::Array(values));
            }
        });

    changed
}

// ---------------------------------------------------------------------------
// Video
// ---------------------------------------------------------------------------

fn video_tab(ui: &mut Ui, doc: &mut Document) -> Action {
    let mut action = Action::None;
    let Some(meta) = selected_clip_meta(doc) else {
        ui.label("Select a timeline clip to edit picture properties.");
        return action;
    };
    if meta.kind != "video" {
        ui.label("Select a video clip to edit picture properties.");
        return action;
    }

    let position = doc.position.max(0.0);
    let canvas_width = num_at(&doc.model, "width", 1920.0);
    let canvas_height = num_at(&doc.model, "height", 1080.0);
    let Some(clip) = doc.selected_clip_mut() else {
        ui.label("Select a timeline clip to edit picture properties.");
        return action;
    };

    let start = num_at(clip, "start", 0.0);
    let local = (position - start).max(0.0);
    let mut placement = None;

    ui.strong("Transform and crop");
    egui::Grid::new(("inspector_video", meta.id.as_str()))
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            number_control(ui, clip, "x", "Left", 0.0, -16384.0, 16384.0, 0.1, 1, local);
            number_control(ui, clip, "y", "Top", 0.0, -16384.0, 16384.0, 0.1, 1, local);
            number_control(ui, clip, "w", "Width", 100.0, 2.0, 16384.0, 0.1, 1, local);
            number_control(ui, clip, "h", "Height", 100.0, 2.0, 16384.0, 0.1, 1, local);
            number_control(
                ui,
                clip,
                "rotation",
                "Rotation (deg)",
                0.0,
                -360.0,
                360.0,
                0.1,
                1,
                local,
            );
            number_control(
                ui, clip, "opacity", "Opacity", 1.0, 0.0, 1.0, 0.01, 2, local,
            );

            ui.label("Placement");
            egui::ComboBox::from_id_salt(("inspector_placement", meta.id.as_str()))
                .selected_text("Layout preset...")
                .show_ui(ui, |ui| {
                    for (index, label) in [
                        "Fit canvas",
                        "Left half",
                        "Right half",
                        "Picture in picture",
                    ]
                    .iter()
                    .enumerate()
                    {
                        if ui.selectable_label(false, *label).clicked() {
                            placement = Some(index + 1);
                        }
                    }
                });
            ui.end_row();
        });

    if let Some(index) = placement {
        apply_layout(clip, index, canvas_width, canvas_height, &meta.source);
    }

    ui.separator();
    ui.strong("Normalized crop");
    let mut crop_x = value_at(clip, "crop_x", local, 0.0);
    let mut crop_y = value_at(clip, "crop_y", local, 0.0);
    let mut crop_w = value_at(clip, "crop_w", local, 1.0);
    let mut crop_h = value_at(clip, "crop_h", local, 1.0);

    let mut crop_x_changed = false;
    let mut crop_w_changed = false;
    let mut crop_y_changed = false;
    let mut crop_h_changed = false;

    egui::Grid::new(("inspector_crop", meta.id.as_str()))
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            ui.label("Crop left (0-1)");
            crop_x_changed = drag_f64(ui, &mut crop_x, 0.0, 1.0, 0.01, 3);
            ui.end_row();
            ui.label("Crop top (0-1)");
            crop_y_changed = drag_f64(ui, &mut crop_y, 0.0, 1.0, 0.01, 3);
            ui.end_row();
            ui.label("Crop width (0-1)");
            crop_w_changed = drag_f64(ui, &mut crop_w, 0.01, 1.0, 0.01, 3);
            ui.end_row();
            ui.label("Crop height (0-1)");
            crop_h_changed = drag_f64(ui, &mut crop_h, 0.01, 1.0, 0.01, 3);
            ui.end_row();
        });

    let crop_changed = crop_x_changed || crop_y_changed || crop_w_changed || crop_h_changed;
    if crop_changed {
        if crop_x_changed {
            crop_x = crop_x.min(1.0 - crop_w);
        }
        if crop_w_changed {
            crop_w = crop_w.min(1.0 - crop_x);
        }
        if crop_y_changed {
            crop_y = crop_y.min(1.0 - crop_h);
        }
        if crop_h_changed {
            crop_h = crop_h.min(1.0 - crop_y);
        }
        crop_x = crop_x.clamp(0.0, 1.0);
        crop_y = crop_y.clamp(0.0, 1.0);
        crop_w = crop_w.clamp(0.01, 1.0);
        crop_h = crop_h.clamp(0.01, 1.0);
        write_property(clip, "crop_x", local, crop_x);
        write_property(clip, "crop_y", local, crop_y);
        write_property(clip, "crop_w", local, crop_w);
        write_property(clip, "crop_h", local, crop_h);
    }
    note(ui, "Crop values are fractions of the source frame.");

    ui.separator();
    egui::CollapsingHeader::new("Color and effects")
        .default_open(false)
        .show(ui, |ui| {
            egui::Grid::new(("inspector_video_color", meta.id.as_str()))
                .num_columns(2)
                .spacing([8.0, 6.0])
                .show(ui, |ui| {
                    number_control(
                        ui,
                        clip,
                        "brightness",
                        "Brightness",
                        0.0,
                        -1.0,
                        1.0,
                        0.01,
                        2,
                        local,
                    );
                    number_control(
                        ui, clip, "contrast", "Contrast", 1.0, 0.0, 3.0, 0.01, 2, local,
                    );
                    number_control(
                        ui,
                        clip,
                        "saturation",
                        "Saturation",
                        1.0,
                        0.0,
                        3.0,
                        0.01,
                        2,
                        local,
                    );
                    number_control(ui, clip, "gamma", "Gamma", 1.0, 0.1, 5.0, 0.01, 2, local);
                    number_control(
                        ui,
                        clip,
                        "temperature",
                        "Warmth",
                        0.0,
                        -0.5,
                        0.5,
                        0.01,
                        2,
                        local,
                    );
                    number_control(ui, clip, "tint", "Tint", 0.0, -0.5, 0.5, 0.01, 2, local);
                    number_control(
                        ui,
                        clip,
                        "sharpness",
                        "Sharpness",
                        0.0,
                        -1.0,
                        3.0,
                        0.01,
                        2,
                        local,
                    );
                    bool_control(ui, clip, "flip_x", "Flip horizontally");
                    bool_control(ui, clip, "flip_y", "Flip vertically");
                    bool_control(ui, clip, "blackout", "Blackout (opaque)");
                    number_control(
                        ui,
                        clip,
                        "border_width",
                        "Border width",
                        0.0,
                        0.0,
                        100.0,
                        0.1,
                        1,
                        local,
                    );

                    ui.label("Border color");
                    let mut color = text_at(clip, "border_color");
                    if color.is_empty() {
                        color = "#ffffff".to_owned();
                    }
                    if color_row(
                        ui,
                        egui::Id::new(("inspector_border_color", meta.id.as_str())),
                        &mut color,
                    ) {
                        set_text(clip, "border_color", &color);
                    }
                    ui.end_row();

                    ui.label("3D LUT");
                    let lut = text_at(clip, "lut");
                    ui.add(
                        egui::Label::new(if lut.is_empty() {
                            "None".to_owned()
                        } else {
                            lut
                        })
                        .truncate(),
                    );
                    ui.end_row();
                });

            ui.horizontal_wrapped(|ui| {
                if ui.button("Choose LUT...").clicked() {
                    set_action(&mut action, Action::Lut);
                }
                if ui.button("Clear LUT").clicked() {
                    if let Some(object) = clip.as_object_mut() {
                        object.remove("lut");
                    }
                }
                if ui.button("Analyze stabilization").clicked() {
                    set_action(&mut action, Action::Stabilize(clip.clone()));
                }
            });
        });

    ui.separator();
    egui::CollapsingHeader::new("Animation keyframes")
        .default_open(false)
        .show(ui, |ui| {
            keyframe_table(ui, clip, &format!("clip:{}", meta.id), VIDEO_KEYS, local);
        });

    action
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

fn audio_tab(ui: &mut Ui, doc: &mut Document) -> Action {
    let Some(meta) = selected_clip_meta(doc) else {
        ui.label("Select a timeline clip to edit audio properties.");
        return Action::None;
    };
    if meta.kind != "audio" {
        ui.label("Select an audio clip to edit audio properties.");
        return Action::None;
    }

    let position = doc.position.max(0.0);
    let Some(clip) = doc.selected_clip_mut() else {
        ui.label("Select a timeline clip to edit audio properties.");
        return Action::None;
    };
    let start = num_at(clip, "start", 0.0);
    let local = (position - start).max(0.0);

    ui.strong("Level and fades");
    let mut fade_changed = false;
    egui::Grid::new(("inspector_audio", meta.id.as_str()))
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            number_control(
                ui,
                clip,
                "gain",
                "Level (0-4)",
                1.0,
                0.0,
                4.0,
                0.01,
                2,
                local,
            );
            let fade_in_changed = number_control(
                ui,
                clip,
                "fade_in",
                "Fade in (s)",
                0.0,
                0.0,
                600.0,
                0.1,
                2,
                local,
            );
            let fade_out_changed = number_control(
                ui,
                clip,
                "fade_out",
                "Fade out (s)",
                0.0,
                0.0,
                600.0,
                0.1,
                2,
                local,
            );
            fade_changed = fade_in_changed || fade_out_changed;
            bool_control(ui, clip, "mute", "Mute");
        });

    egui::CollapsingHeader::new("Audio cleanup")
        .default_open(false)
        .show(ui, |ui| {
            egui::Grid::new(("inspector_audio_cleanup", meta.id.as_str()))
                .num_columns(2)
                .show(ui, |ui| {
                    bool_control(ui, clip, "denoise", "Denoise");
                    bool_control(ui, clip, "normalize", "Normalize loudness");
                });
        });

    if fade_changed {
        let total = clip_duration(clip);
        set_num(clip, "effect_offset", 0.0);
        set_num(clip, "effect_length", total);
    }

    note(
        ui,
        "Level uses 0-4. Fades and keyframe times are seconds from the clip start.",
    );

    ui.separator();
    egui::CollapsingHeader::new("Gain keyframes")
        .default_open(false)
        .show(ui, |ui| {
            keyframe_table(ui, clip, &format!("audio:{}", meta.id), AUDIO_KEYS, local);
        });

    Action::None
}

// ---------------------------------------------------------------------------
// Objects, annotations and privacy regions
// ---------------------------------------------------------------------------

fn objects_tab(
    ui: &mut Ui,
    doc: &mut Document,
    selected_annotation: &mut Option<String>,
) -> Action {
    let mut action = Action::None;
    let position = doc.position.max(0.0);
    let timeline = duration(&doc.model).max(0.0);
    let annotations = doc
        .model
        .get("annotations")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);

    ui.strong("Objects");
    if annotations.is_empty() {
        ui.label("No titles, drawings or privacy regions yet.");
    } else {
        for annotation in annotations {
            let Some(id) = annotation.get("id").and_then(Value::as_str) else {
                continue;
            };
            let kind = annotation
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("object");
            let name = if kind == "text" {
                let text = text_at(annotation, "text");
                let short = text.chars().take(28).collect::<String>();
                if short.is_empty() {
                    "Text".to_owned()
                } else {
                    short
                }
            } else {
                capitalize(kind)
            };
            let summary = format!(
                "{} - {} to {}",
                name,
                timecode(num_at(annotation, "start", 0.0)),
                timecode(num_at(annotation, "end", 0.0))
            );
            if ui
                .selectable_label(selected_annotation.as_deref() == Some(id), summary)
                .clicked()
            {
                *selected_annotation = Some(id.to_owned());
                doc.selected.clear();
                doc.primary = None;
            }
        }
    }

    ui.separator();
    ui.horizontal_wrapped(|ui| {
        if ui.button("Add text").clicked() {
            set_action(&mut action, Action::AddText);
        }
    });

    let kind_id = egui::Id::new("inspector_privacy_kind");
    let mut privacy_kind = ui
        .data_mut(|data| *data.get_temp_mut_or_insert_with(kind_id, || 0usize))
        .min(2);
    ui.horizontal_wrapped(|ui| {
        ui.label("Privacy region");
        egui::ComboBox::from_id_salt("inspector_privacy_combo")
            .selected_text(["Blackout", "Blur", "Pixelate"][privacy_kind])
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut privacy_kind, 0usize, "Blackout");
                ui.selectable_value(&mut privacy_kind, 1usize, "Blur");
                ui.selectable_value(&mut privacy_kind, 2usize, "Pixelate");
            });
        if ui.button("Add privacy region").clicked() {
            let id = uid();
            let kind = match privacy_kind {
                1 => "blur",
                2 => "pixelate",
                _ => "redact",
            };
            let width = num_at(&doc.model, "width", 1920.0);
            let height = num_at(&doc.model, "height", 1080.0);
            let annotation = json!({
                "id": id,
                "kind": kind,
                "start": position,
                "end": (position + 1.0).max(timeline),
                "x": width * 0.25,
                "y": height * 0.25,
                "w": width * 0.5,
                "h": height * 0.5,
                "keys": {}
            });
            if let Some(array) = doc
                .model
                .get_mut("annotations")
                .and_then(Value::as_array_mut)
            {
                array.push(annotation);
            } else {
                set_value(
                    &mut doc.model,
                    "annotations",
                    Value::Array(vec![annotation]),
                );
            }
            *selected_annotation = Some(id);
            doc.selected.clear();
            doc.primary = None;
        }
    });
    ui.data_mut(|data| {
        *data.get_temp_mut_or_insert_with(kind_id, || 0usize) = privacy_kind;
    });

    ui.separator();
    let Some(selected_id) = selected_annotation.clone() else {
        ui.label("Select an object to edit its time, size, text and color.");
        return action;
    };
    let index = doc
        .model
        .get("annotations")
        .and_then(Value::as_array)
        .and_then(|items| {
            items.iter().position(|item| {
                item.get("id").and_then(Value::as_str) == Some(selected_id.as_str())
            })
        });
    let Some(index) = index else {
        *selected_annotation = None;
        ui.label("The selected object no longer exists.");
        return action;
    };

    let mut delete = false;
    let mut track_value = None;

    {
        let Some(annotation) = doc
            .model
            .get_mut("annotations")
            .and_then(Value::as_array_mut)
            .and_then(|items| items.get_mut(index))
        else {
            *selected_annotation = None;
            return action;
        };

        let kind = text_at(annotation, "kind");
        let privacy = matches!(kind.as_str(), "redact" | "blur" | "pixelate");
        let text_object = kind == "text";
        let start = num_at(annotation, "start", 0.0);
        let local = (position - start).max(0.0);

        ui.strong("Object properties");
        egui::Grid::new(("inspector_object", selected_id.as_str()))
            .num_columns(2)
            .spacing([8.0, 6.0])
            .show(ui, |ui| {
                ui.label("Kind");
                ui.label(capitalize(&kind));
                ui.end_row();

                let start_changed = number_control(
                    ui,
                    annotation,
                    "start",
                    "Start (s)",
                    0.0,
                    0.0,
                    100000.0,
                    0.01,
                    3,
                    local,
                );
                let end_changed = number_control(
                    ui, annotation, "end", "End (s)", 1.0, 0.0, 100000.0, 0.01, 3, local,
                );
                if start_changed || end_changed {
                    let current_start = num_at(annotation, "start", 0.0);
                    let current_end = num_at(annotation, "end", 1.0);
                    if current_end <= current_start {
                        set_num(annotation, "end", current_start + 0.01);
                    }
                }

                number_control(
                    ui, annotation, "x", "Left", 0.0, -16384.0, 16384.0, 0.1, 1, local,
                );
                number_control(
                    ui, annotation, "y", "Top", 0.0, -16384.0, 16384.0, 0.1, 1, local,
                );
                number_control(
                    ui, annotation, "w", "Width", 100.0, 1.0, 16384.0, 0.1, 1, local,
                );
                number_control(
                    ui, annotation, "h", "Height", 100.0, 1.0, 16384.0, 0.1, 1, local,
                );

                if !privacy {
                    number_control(
                        ui, annotation, "opacity", "Opacity", 1.0, 0.0, 1.0, 0.01, 2, local,
                    );
                }
                if !privacy {
                    ui.label("Color");
                    let mut color = text_at(annotation, "color");
                    if color.is_empty() {
                        color = "#ffffff".to_owned();
                    }
                    if color_row(
                        ui,
                        egui::Id::new(("inspector_object_color", selected_id.as_str())),
                        &mut color,
                    ) {
                        set_text(annotation, "color", &color);
                    }
                    ui.end_row();
                }
                if text_object {
                    ui.label("Text");
                    let mut text = text_at(annotation, "text");
                    let changed = ui
                        .add(
                            egui::TextEdit::multiline(&mut text)
                                .desired_rows(3)
                                .desired_width(160.),
                        )
                        .changed();
                    if changed {
                        set_text(annotation, "text", &text);
                    }
                    ui.end_row();
                }
            });

        if text_object {
            egui::CollapsingHeader::new("Font properties")
                .default_open(false)
                .show(ui, |ui| {
                    egui::Grid::new(("inspector_object_font", selected_id.as_str()))
                        .num_columns(2)
                        .show(ui, |ui| {
                            number_control(
                                ui,
                                annotation,
                                "font_size",
                                "Font size",
                                48.0,
                                1.0,
                                1000.0,
                                0.1,
                                1,
                                local,
                            );
                            text_control(ui, annotation, "font", "Font");
                        });
                });
        }

        let local_after = (position - num_at(annotation, "start", 0.0)).max(0.0);
        egui::CollapsingHeader::new("Object keyframes")
            .default_open(false)
            .show(ui, |ui| {
                let properties = if privacy { MASK_KEYS } else { ANNOTATION_KEYS };
                keyframe_table(
                    ui,
                    annotation,
                    &format!("object:{}", selected_id),
                    properties,
                    local_after,
                );
            });

        ui.horizontal_wrapped(|ui| {
            if privacy && ui.button("Track selected privacy region").clicked() {
                track_value = Some(annotation.clone());
            }
            if ui.button("Delete selected object").clicked() {
                delete = true;
            }
        });

        if privacy {
            note(
                ui,
                "Tracking estimates must be reviewed over the full range before export.",
            );
        }
    }

    if delete {
        if let Some(array) = doc
            .model
            .get_mut("annotations")
            .and_then(Value::as_array_mut)
        {
            if index < array.len() {
                array.remove(index);
            }
        }
        *selected_annotation = None;
    }

    if let Some(value) = track_value {
        set_action(&mut action, Action::Track(value));
    }

    action
}

// ---------------------------------------------------------------------------
// Captions
// ---------------------------------------------------------------------------

fn captions_tab(ui: &mut Ui, doc: &mut Document) -> Action {
    let mut action = Action::None;
    let position = doc.position.max(0.0);

    ui.strong("Subtitles");
    let count = doc
        .model
        .get("subtitles")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);

    if count == 0 {
        ui.label("No subtitle rows yet.");
    } else {
        let mut remove = Vec::new();
        egui::Grid::new("inspector_subtitles")
            .num_columns(4)
            .striped(true)
            .spacing([6.0, 4.0])
            .show(ui, |ui| {
                ui.strong("Start (s)");
                ui.strong("End (s)");
                ui.strong("Text");
                ui.strong("");
                ui.end_row();

                for index in 0..count {
                    let Some(row) = doc
                        .model
                        .get_mut("subtitles")
                        .and_then(Value::as_array_mut)
                        .and_then(|items| items.get_mut(index))
                    else {
                        continue;
                    };

                    let mut start = num_at(row, "start", 0.0);
                    let mut end = num_at(row, "end", start + 2.0);
                    let mut text = text_at(row, "text");
                    let start_changed = drag_f64(ui, &mut start, 0.0, 100000.0, 0.01, 3);
                    let end_changed = drag_f64(ui, &mut end, 0.0, 100000.0, 0.01, 3);
                    let text_changed = ui
                        .add(egui::TextEdit::singleline(&mut text).desired_width(180.0))
                        .changed();

                    if start_changed || end_changed || text_changed {
                        if end <= start {
                            end = start + 0.01;
                        }
                        set_num(row, "start", start);
                        set_num(row, "end", end);
                        if text_changed {
                            set_text(row, "text", &text);
                        }
                    }

                    if ui.small_button("Remove").clicked() {
                        remove.push(index);
                    }
                    ui.end_row();
                }
            });

        if !remove.is_empty() {
            if let Some(array) = doc.model.get_mut("subtitles").and_then(Value::as_array_mut) {
                for index in remove.into_iter().rev() {
                    if index < array.len() {
                        array.remove(index);
                    }
                }
            }
        }
    }

    if ui.button("Add subtitle row").clicked() {
        let row = json!({
            "id": uid(),
            "start": position,
            "end": (position + 2.0).max(position + 0.01),
            "text": "Caption"
        });
        if let Some(array) = doc.model.get_mut("subtitles").and_then(Value::as_array_mut) {
            array.push(row);
        } else {
            set_value(&mut doc.model, "subtitles", Value::Array(vec![row]));
        }
    }

    ui.separator();
    egui::CollapsingHeader::new("Caption style")
        .default_open(false)
        .show(ui, |ui| {
            let original_style = doc
                .model
                .get("subtitle_style")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let mut style = original_style.clone();
            egui::Grid::new("inspector_caption_style")
                .num_columns(2)
                .spacing([8.0, 6.0])
                .show(ui, |ui| {
                    text_control(ui, &mut style, "font", "Font");
                    number_control(
                        ui, &mut style, "size", "Size", 36.0, 8.0, 1000.0, 1.0, 0, 0.0,
                    );
                    ui.label("Color");
                    let mut color = text_at(&style, "color");
                    if color.is_empty() {
                        color = "#ffffff".to_owned();
                    }
                    if color_row(ui, egui::Id::new("inspector_caption_color"), &mut color) {
                        set_text(&mut style, "color", &color);
                    }
                    ui.end_row();
                });
            if style != original_style {
                set_value(&mut doc.model, "subtitle_style", style);
            }
        });

    ui.separator();
    ui.horizontal_wrapped(|ui| {
        if ui.button("Import SRT...").clicked() {
            set_action(&mut action, Action::ImportSrt);
        }
        if ui.button("Export SRT...").clicked() {
            set_action(&mut action, Action::ExportSrt);
        }
    });

    ui.separator();
    ui.strong("Automatic subtitles");
    let language_id = egui::Id::new("inspector_transcribe_language");
    let mut language = ui
        .data_mut(|data| *data.get_temp_mut_or_insert_with(language_id, || 0usize))
        .min(LANGUAGES.len() - 1);
    ui.horizontal_wrapped(|ui| {
        egui::ComboBox::from_id_salt("inspector_language")
            .selected_text(LANGUAGES[language].0)
            .show_ui(ui, |ui| {
                for (index, (label, _)) in LANGUAGES.iter().enumerate() {
                    ui.selectable_value(&mut language, index, *label);
                }
            });
        if ui.button("Transcribe speech...").clicked() {
            set_action(
                &mut action,
                Action::Transcribe(LANGUAGES[language].1.to_owned()),
            );
        }
    });
    ui.data_mut(|data| {
        *data.get_temp_mut_or_insert_with(language_id, || 0usize) = language;
    });
    note(
        ui,
        "Transcription runs locally without uploading audio. Review generated text and timing.",
    );

    action
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

fn canvas_tab(ui: &mut Ui, doc: &mut Document, snapshot: &mut EditSnapshot) -> Action {
    let old_width = num_at(&doc.model, "width", 1920.0);
    let old_height = num_at(&doc.model, "height", 1080.0);
    let mut width = old_width.round() as i64;
    let mut height = old_height.round() as i64;
    let mut fps = num_at(&doc.model, "fps", 30.0).round() as i64;

    let scale_id = egui::Id::new("inspector_canvas_scale");
    let mut scale = ui.data_mut(|data| *data.get_temp_mut_or_insert_with(scale_id, || true));

    let mut size_changed = false;
    let mut fps_changed = false;
    let mut preset = None;

    ui.strong("Canvas");
    egui::Grid::new("inspector_canvas")
        .num_columns(2)
        .spacing([8.0, 6.0])
        .show(ui, |ui| {
            ui.label("Width");
            size_changed |= drag_i64(ui, &mut width, 2, 16384, 1.0);
            ui.end_row();
            ui.label("Height");
            size_changed |= drag_i64(ui, &mut height, 2, 16384, 1.0);
            ui.end_row();
            ui.label("Frame rate");
            fps_changed |= drag_i64(ui, &mut fps, 1, 240, 1.0);
            ui.end_row();
        });

    ui.horizontal_wrapped(|ui| {
        ui.label("Preset");
        if ui.button("Landscape 1920 x 1080").clicked() {
            preset = Some((1920, 1080));
        }
        if ui.button("Portrait 1080 x 1920").clicked() {
            preset = Some((1080, 1920));
        }
        if ui.button("Square 1080 x 1080").clicked() {
            preset = Some((1080, 1080));
        }
    });

    if let Some((preset_width, preset_height)) = preset {
        width = preset_width;
        height = preset_height;
        size_changed = true;
    }

    let new_width = ((width as f64 / 2.0).round() as i64 * 2).clamp(2, 16384);
    let new_height = ((height as f64 / 2.0).round() as i64 * 2).clamp(2, 16384);
    let new_fps = fps.clamp(1, 240);

    if size_changed {
        let scale_x = new_width as f64 / old_width.max(2.0);
        let scale_y = new_height as f64 / old_height.max(2.0);
        set_int(&mut doc.model, "width", new_width);
        set_int(&mut doc.model, "height", new_height);
        if scale {
            snapshot.field(&doc.model, "tracks");
            snapshot.field(&doc.model, "annotations");
            scale_contents(&mut doc.model, scale_x, scale_y);
        }
    }
    if fps_changed {
        set_int(&mut doc.model, "fps", new_fps);
    }

    ui.checkbox(&mut scale, "Scale existing contents with canvas changes");
    ui.data_mut(|data| {
        *data.get_temp_mut_or_insert_with(scale_id, || true) = scale;
    });
    note(ui, "Canvas dimensions are rounded to even numbers.");

    ui.separator();
    ui.strong("Background");
    let mut background = text_at(&doc.model, "background");
    if background.is_empty() {
        background = "#000000".to_owned();
    }
    ui.horizontal_wrapped(|ui| {
        ui.label("Canvas color");
        if color_row(
            ui,
            egui::Id::new("inspector_canvas_background"),
            &mut background,
        ) {
            set_text(&mut doc.model, "background", &background);
        }
    });
    note(ui, "Background colors are saved as #rrggbb.");

    Action::None
}

// ---------------------------------------------------------------------------
// Shared controls and JSON helpers
// ---------------------------------------------------------------------------

fn selected_clip_meta(doc: &Document) -> Option<SelectedClip> {
    let clip = doc.selected_clip()?;
    let id = clip.get("id").and_then(Value::as_str)?.to_owned();
    let source_id = clip
        .get("source_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let source = source_by_id(&doc.project, source_id).unwrap_or(Value::Null);
    let kind = track_kind(&doc.model, &id)
        .or_else(|| {
            source
                .get("type")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_default()
        .to_ascii_lowercase();
    Some(SelectedClip { id, kind, source })
}

fn track_kind(model: &Value, clip_id: &str) -> Option<String> {
    for track in model.get("tracks")?.as_array()? {
        let Some(clips) = track.get("clips").and_then(Value::as_array) else {
            continue;
        };
        if clips
            .iter()
            .any(|clip| clip.get("id").and_then(Value::as_str) == Some(clip_id))
        {
            return track.get("kind").and_then(Value::as_str).map(str::to_owned);
        }
    }
    None
}

fn source_by_id(project: &Value, id: &str) -> Option<Value> {
    project
        .get("sources")?
        .as_array()?
        .iter()
        .find(|source| source.get("id").and_then(Value::as_str) == Some(id))
        .cloned()
}

fn number_control(
    ui: &mut Ui,
    item: &mut Value,
    key: &str,
    label: &str,
    default: f64,
    min: f64,
    max: f64,
    speed: f64,
    decimals: usize,
    local: f64,
) -> bool {
    let mut value = value_at(item, key, local, default);
    ui.label(label);
    let changed = drag_f64(ui, &mut value, min, max, speed, decimals);
    if changed {
        write_property(item, key, local, value);
    }
    ui.end_row();
    changed
}

fn text_control(ui: &mut Ui, item: &mut Value, key: &str, label: &str) -> bool {
    let mut text = text_at(item, key);
    ui.label(label);
    let changed = ui.text_edit_singleline(&mut text).changed();
    if changed {
        set_text(item, key, &text);
    }
    ui.end_row();
    changed
}

fn bool_control(ui: &mut Ui, item: &mut Value, key: &str, label: &str) -> bool {
    let mut checked = truthy(item.get(key));
    ui.label(label);
    let changed = ui
        .add(egui::Checkbox::without_text(&mut checked))
        .on_hover_text(label)
        .changed();
    if changed {
        set_bool(item, key, checked);
    }
    ui.end_row();
    changed
}

fn drag_f64(ui: &mut Ui, value: &mut f64, min: f64, max: f64, speed: f64, decimals: usize) -> bool {
    let response = ui.add(
        egui::DragValue::new(value)
            .speed(speed)
            .range(min..=max)
            .fixed_decimals(decimals),
    );
    let changed = response.changed();
    if changed {
        *value = value.clamp(min, max);
    }
    changed
}

fn drag_i64(ui: &mut Ui, value: &mut i64, min: i64, max: i64, speed: f64) -> bool {
    let response = ui.add(
        egui::DragValue::new(value)
            .speed(speed)
            .range(min..=max)
            .fixed_decimals(0),
    );
    let changed = response.changed();
    if changed {
        *value = (*value).clamp(min, max);
    }
    changed
}

fn color_row(ui: &mut Ui, id: egui::Id, value: &mut String) -> bool {
    ui.horizontal(|ui| {
    let mut changed = false;
    let mut rgb = parse_hex(value).unwrap_or([255, 255, 255]);
    if ui.color_edit_button_srgb(&mut rgb).changed() {
        *value = format_hex(rgb);
        changed = true;
    }

    let text_id = id.with("hex");
    let mut text = ui.data_mut(|data| {
        data.get_temp_mut_or_insert_with(text_id, || value.clone())
            .clone()
    });
    let response = ui.add(
        egui::TextEdit::singleline(&mut text)
            .desired_width(90.0)
            .hint_text("#rrggbb"),
    );
    if response.changed() {
        if let Some(rgb) = parse_hex(&text) {
            *value = format_hex(rgb);
            changed = true;
        }
    } else if !response.has_focus() {
        text = value.clone();
    }
    ui.data_mut(|data| {
        *data.get_temp_mut_or_insert_with(text_id, || value.clone()) = text;
    });
    changed
    }).inner
}

fn keyframe_table(
    ui: &mut Ui,
    item: &mut Value,
    scope: &str,
    properties: &[KeyProperty],
    local: f64,
) -> bool {
    if properties.is_empty() {
        return false;
    }

    let property_id = egui::Id::new(("inspector_kf_property", scope));
    let mut property = ui.data_mut(|data| {
        data.get_temp_mut_or_insert_with(property_id, || properties[0].key.to_owned())
            .clone()
    });

    ui.horizontal_wrapped(|ui| {
        ui.label("Property");
        egui::ComboBox::from_id_salt(("inspector_kf_combo", scope))
            .selected_text(
                properties
                    .iter()
                    .find(|spec| spec.key == property.as_str())
                    .map(|spec| spec.label)
                    .unwrap_or(properties[0].label),
            )
            .show_ui(ui, |ui| {
                for spec in properties {
                    ui.selectable_value(&mut property, spec.key.to_owned(), spec.label);
                }
            });
    });
    ui.data_mut(|data| {
        *data.get_temp_mut_or_insert_with(property_id, || properties[0].key.to_owned()) =
            property.clone();
    });

    let spec = properties
        .iter()
        .find(|spec| spec.key == property.as_str())
        .copied()
        .unwrap_or(properties[0]);
    let mut rows = pairs(item.get("keys").and_then(|keys| keys.get(spec.key)));
    let mut changed = false;

    if rows.is_empty() {
        ui.label("No keyframes for this property.");
    } else {
        egui::Grid::new(("inspector_kf_rows", scope, spec.key))
            .num_columns(3)
            .striped(true)
            .spacing([6.0, 4.0])
            .show(ui, |ui| {
                ui.strong("Time (s)");
                ui.strong("Value");
                ui.strong("");
                ui.end_row();

                let mut remove = None;
                for (index, row) in rows.iter_mut().enumerate() {
                    let time_changed = drag_f64(ui, &mut row.0, 0.0, 100000.0, 0.01, 3);
                    let value_changed =
                        drag_f64(ui, &mut row.1, spec.min, spec.max, 0.01, spec.decimals);
                    if time_changed || value_changed {
                        changed = true;
                    }
                    if ui.small_button("Remove").clicked() {
                        remove = Some(index);
                    }
                    ui.end_row();
                }
                if let Some(index) = remove {
                    rows.remove(index);
                    changed = true;
                }
            });
    }

    if ui.button("Add key at playhead").clicked() {
        let value = value_at(item, spec.key, local, spec.default);
        rows.push((local.max(0.0), value.clamp(spec.min, spec.max)));
        changed = true;
    }
    note(
        ui,
        "Times are seconds from the clip or object start. Values interpolate linearly.",
    );

    if changed {
        rows.sort_by(|left, right| {
            left.0
                .partial_cmp(&right.0)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        write_key_rows(item, spec.key, &rows);
    }

    changed
}

fn write_property(item: &mut Value, key: &str, local: f64, value: f64) {
    if key_has_points(item, key) {
        write_key(item, key, local, value);
    } else {
        set_num(item, key, value);
    }
}

fn write_key(item: &mut Value, key: &str, time: f64, value: f64) {
    let mut rows = pairs(item.get("keys").and_then(|keys| keys.get(key)));
    rows.retain(|(existing, _)| (existing - time).abs() > 0.001);
    rows.push((time, value));
    rows.sort_by(|left, right| {
        left.0
            .partial_cmp(&right.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    write_key_rows(item, key, &rows);
}

fn write_key_rows(item: &mut Value, key: &str, rows: &[(f64, f64)]) {
    let Some(object) = item.as_object_mut() else {
        return;
    };
    if !object.get("keys").is_some_and(|keys| keys.is_object()) {
        object.insert("keys".to_owned(), Value::Object(Map::new()));
    }
    let Some(keys) = object.get_mut("keys").and_then(Value::as_object_mut) else {
        return;
    };
    if rows.is_empty() {
        keys.remove(key);
    } else {
        let values = rows
            .iter()
            .map(|(time, value)| json!([*time, *value]))
            .collect::<Vec<_>>();
        keys.insert(key.to_owned(), Value::Array(values));
    }
}

fn key_has_points(item: &Value, key: &str) -> bool {
    item.get("keys")
        .and_then(Value::as_object)
        .and_then(|keys| keys.get(key))
        .and_then(Value::as_array)
        .is_some_and(|points| !points.is_empty())
}

fn pairs(value: Option<&Value>) -> Vec<(f64, f64)> {
    let mut rows = Vec::new();
    if let Some(Value::Array(items)) = value {
        for item in items {
            if let Some(pair) = item.as_array()
                && pair.len() >= 2
                && let (Some(time), Some(amount)) = (to_f64(&pair[0]), to_f64(&pair[1]))
            {
                rows.push((time, amount));
            }
        }
    }
    rows
}

fn apply_layout(
    clip: &mut Value,
    index: usize,
    canvas_width: f64,
    canvas_height: f64,
    source: &Value,
) {
    let source_width = source
        .get("width")
        .and_then(to_f64)
        .unwrap_or(canvas_width)
        .max(1.0);
    let source_height = source
        .get("height")
        .and_then(to_f64)
        .unwrap_or(canvas_height)
        .max(1.0);

    if index <= 3 {
        let (region_width, region_x) = match index {
            1 => (canvas_width, 0.0),
            2 => (canvas_width / 2.0, 0.0),
            _ => (canvas_width / 2.0, canvas_width / 2.0),
        };
        let factor = (region_width / source_width).min(canvas_height / source_height);
        let width = source_width * factor;
        let height = source_height * factor;
        set_num(clip, "x", region_x + (region_width - width) / 2.0);
        set_num(clip, "y", (canvas_height - height) / 2.0);
        set_num(clip, "w", width.max(2.0));
        set_num(clip, "h", height.max(2.0));
    } else {
        let aspect = source_width / source_height;
        let width = canvas_width * 0.3;
        set_num(clip, "x", canvas_width * 0.65);
        set_num(clip, "y", canvas_height * 0.65);
        set_num(clip, "w", width.max(2.0));
        set_num(clip, "h", (width / aspect).max(2.0));
    }

    if let Some(object) = clip.as_object_mut() {
        if let Some(keys) = object.get_mut("keys").and_then(Value::as_object_mut) {
            for key in ["x", "y", "w", "h"] {
                keys.remove(key);
            }
        }
    }
}

fn scale_contents(model: &mut Value, scale_x: f64, scale_y: f64) {
    if let Some(tracks) = model.get_mut("tracks").and_then(Value::as_array_mut) {
        for track in tracks {
            if let Some(clips) = track.get_mut("clips").and_then(Value::as_array_mut) {
                for clip in clips {
                    scale_object(clip, scale_x, scale_y);
                }
            }
        }
    }
    if let Some(annotations) = model.get_mut("annotations").and_then(Value::as_array_mut) {
        for annotation in annotations {
            scale_object(annotation, scale_x, scale_y);
        }
    }
}

fn scale_object(object: &mut Value, scale_x: f64, scale_y: f64) {
    for (key, factor) in [
        ("x", scale_x),
        ("y", scale_y),
        ("w", scale_x),
        ("h", scale_y),
    ] {
        let value = object.get(key).and_then(to_f64);
        if let Some(value) = value {
            set_num(object, key, value * factor);
        }
        if let Some(keys) = object.get_mut("keys").and_then(Value::as_object_mut) {
            if let Some(Value::Array(points)) = keys.get_mut(key) {
                for point in points {
                    if let Some(pair) = point.as_array_mut()
                        && pair.len() >= 2
                        && let Some(value) = to_f64(&pair[1])
                    {
                        pair[1] = number(value * factor);
                    }
                }
            }
        }
    }
}

fn set_action(slot: &mut Action, candidate: Action) {
    if matches!(slot, Action::None) {
        *slot = candidate;
    }
}

fn note(ui: &mut Ui, text: &str) {
    ui.add(egui::Label::new(egui::RichText::new(text).small()).wrap());
}

fn capitalize(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

fn timecode(seconds: f64) -> String {
    let total = seconds.max(0.0) as i64;
    let (hours, remainder) = (total / 3600, total % 3600);
    let (minutes, seconds) = (remainder / 60, remainder % 60);
    if hours > 0 {
        format!("{hours:02}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

fn to_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }
}

fn num_at(item: &Value, key: &str, default: f64) -> f64 {
    item.get(key).and_then(to_f64).unwrap_or(default)
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64().is_some_and(|number| number != 0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
        _ => false,
    }
}

fn text_at(item: &Value, key: &str) -> String {
    item.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn number(value: f64) -> Value {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn set_num(item: &mut Value, key: &str, value: f64) {
    if let Some(object) = item.as_object_mut() {
        object.insert(key.to_owned(), number(value));
    }
}

fn set_int(item: &mut Value, key: &str, value: i64) {
    if let Some(object) = item.as_object_mut() {
        object.insert(key.to_owned(), Value::Number(value.into()));
    }
}

fn set_bool(item: &mut Value, key: &str, value: bool) {
    if let Some(object) = item.as_object_mut() {
        object.insert(key.to_owned(), Value::Bool(value));
    }
}

fn set_text(item: &mut Value, key: &str, value: &str) {
    if let Some(object) = item.as_object_mut() {
        object.insert(key.to_owned(), Value::String(value.to_owned()));
    }
}

fn set_value(item: &mut Value, key: &str, value: Value) {
    if let Some(object) = item.as_object_mut() {
        object.insert(key.to_owned(), value);
    }
}

fn parse_hex(text: &str) -> Option<[u8; 3]> {
    let value = text.trim().trim_start_matches('#');
    if !value.is_ascii() {
        return None;
    }
    let (red, green, blue) = match value.len() {
        3 => {
            let mut chars = value.chars();
            let red = chars.next()?.to_digit(16)? as u8;
            let green = chars.next()?.to_digit(16)? as u8;
            let blue = chars.next()?.to_digit(16)? as u8;
            (red * 17, green * 17, blue * 17)
        }
        6 => (
            u8::from_str_radix(&value[0..2], 16).ok()?,
            u8::from_str_radix(&value[2..4], 16).ok()?,
            u8::from_str_radix(&value[4..6], 16).ok()?,
        ),
        _ => return None,
    };
    Some([red, green, blue])
}

fn format_hex([red, green, blue]: [u8; 3]) -> String {
    format!("#{red:02x}{green:02x}{blue:02x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document() -> (tempfile::TempDir, Document) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("project.katman");
        let project = json!({
            "format": "katman", "version": 1, "sources": [],
            "studio": {
                "width": 1920, "height": 1080, "fps": 30,
                "tracks": [{"kind": "video", "clips": [
                    {"id": "clip", "name": "Clip", "start": 0., "length": 3., "duration": 3., "x": 10., "keys": {}, "extension": {"keep": true}},
                    {"id": "other", "start": 4., "length": 2., "duration": 2.}
                ]}],
                "annotations": [], "subtitles": [], "extension": {"keep": [1, 2, 3]}
            }
        });
        std::fs::write(&path, serde_json::to_vec(&project).unwrap()).unwrap();
        let mut doc = Document::load(&path).unwrap();
        doc.selected.insert("clip".into());
        doc.primary = Some("clip".into());
        (directory, doc)
    }

    #[test]
    fn clip_snapshot_is_scoped_and_gesture_undo_restores_original() {
        let (_directory, mut doc) = document();
        let original = doc.model.clone();
        let snapshot = EditSnapshot::capture(&doc, "Video");
        assert_eq!(snapshot.0.len(), 1);
        assert_eq!(snapshot.0[0].0, "/tracks/0/clips/0");
        doc.selected_clip_mut().unwrap()["x"] = json!(20.);
        assert!(snapshot.commit(&mut doc, true));
        let snapshot = EditSnapshot::capture(&doc, "Video");
        doc.selected_clip_mut().unwrap()["x"] = json!(30.);
        assert!(snapshot.commit(&mut doc, false));
        let edited = doc.model.clone();
        doc.undo();
        assert_eq!(doc.model, original);
        doc.redo();
        assert_eq!(doc.model, edited);
        doc.undo();
        let revision = doc.revision;
        doc.undo(); // Coalesced gesture made only one checkpoint.
        assert_eq!(doc.revision, revision);
    }

    #[test]
    fn canvas_scaling_and_missing_fields_round_trip_through_undo() {
        let (_directory, mut doc) = document();
        let original = doc.model.clone();
        let mut snapshot = EditSnapshot::capture(&doc, "Canvas");
        assert!(snapshot.0.iter().all(|(path, _)| path != "/tracks"));
        snapshot.field(&doc.model, "tracks");
        snapshot.field(&doc.model, "annotations");
        set_int(&mut doc.model, "width", 960);
        set_text(&mut doc.model, "background", "#ffffff");
        scale_contents(&mut doc.model, 0.5, 1.0);
        assert!(snapshot.commit(&mut doc, true));
        let edited = doc.model.clone();
        doc.undo();
        assert_eq!(doc.model, original);
        assert!(doc.model.get("background").is_none());
        doc.redo();
        assert_eq!(doc.model, edited);
    }

    #[test]
    fn idle_inspector_tabs_leave_model_history_and_revision_untouched() {
        let (_directory, mut doc) = document();
        let original = doc.model.clone();
        let context = egui::Context::default();
        let mut selected_annotation = None;
        for name in ["Clip", "Video", "Audio", "Objects", "Captions", "Canvas"] {
            let mut tab = name.to_owned();
            for _ in 0..2 {
                let _ = context.run(egui::RawInput::default(), |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        show(ui, &mut doc, &mut tab, &mut selected_annotation);
                    });
                });
            }
            assert_eq!(doc.model, original, "idle {name} changed the model");
            assert_eq!(doc.revision, 0, "idle {name} changed revision");
        }
        doc.undo();
        assert_eq!(doc.revision, 0, "idle inspection created history");
    }

    #[test]
    fn object_and_caption_edits_preserve_unknown_fields() {
        for tab in ["Objects", "Captions"] {
            let (_directory, mut doc) = document();
            let before = doc.model.clone();
            let snapshot = EditSnapshot::capture(&doc, tab);
            if tab == "Objects" {
                doc.model["annotations"] = json!([{"id": "new", "kind": "text", "text": "Hello"}]);
            } else {
                doc.model["subtitles"] = json!([{"start": 0., "end": 1., "text": "Hello"}]);
                doc.model["subtitle_style"] = json!({"font": "Sans"});
            }
            assert!(snapshot.commit(&mut doc, true));
            let after = doc.model.clone();
            assert_eq!(after["extension"], before["extension"]);
            doc.undo();
            assert_eq!(doc.model, before);
            doc.redo();
            assert_eq!(doc.model, after);
        }
    }
}
