// SPDX-License-Identifier: GPL-3.0-only
//! Native advanced GSR preference fields. This module edits only the supplied
//! draft; the parent owns validation, persistence and capture actions.
use eframe::egui::{self, Ui};
use serde_json::{Value, json};

/// Return true only when a valid field changes the draft. Editing invalid JSON
/// retains the text and displays an inline error without replacing valid data.
pub fn show(ui: &mut Ui, draft: &mut Value) -> bool {
    if !draft.is_object() {
        ui.colored_label(
            ui.visuals().error_fg_color,
            "Advanced capture settings must be a JSON object.",
        );
        return false;
    }
    let mut changed = false;
    ui.label("Default keeps the current recording defaults.");
    egui::CollapsingHeader::new("Video encoding")
        .id_salt("advanced_capture_video")
        .default_open(true)
        .show(ui, |ui| {
            grid(ui, "video", |ui| {
                changed |= choice(
                    ui,
                    draft,
                    "encoder",
                    "Encoder",
                    &[("gpu", "GPU"), ("cpu", "CPU")],
                );
                changed |= choice(
                    ui,
                    draft,
                    "bitrate_mode",
                    "Bitrate mode",
                    &[
                        ("auto", "Automatic"),
                        ("qp", "Constant quality (QP)"),
                        ("vbr", "Variable bitrate (VBR)"),
                        ("cbr", "Constant bitrate (CBR)"),
                    ],
                );
                changed |= choice(
                    ui,
                    draft,
                    "framerate_mode",
                    "Frame rate mode",
                    &[
                        ("cfr", "Constant (CFR)"),
                        ("vfr", "Variable (VFR)"),
                        ("content", "Content driven"),
                    ],
                );
                changed |= choice(
                    ui,
                    draft,
                    "pixel_format",
                    "Pixel format",
                    &[("yuv420", "YUV 4:2:0"), ("yuv444", "YUV 4:4:4")],
                );
                changed |= choice(
                    ui,
                    draft,
                    "color_range",
                    "Color range",
                    &[("limited", "Limited"), ("full", "Full")],
                );
                changed |= choice(
                    ui,
                    draft,
                    "tune",
                    "Tuning",
                    &[("performance", "Performance"), ("quality", "Quality")],
                );
                changed |= number(
                    ui,
                    draft,
                    "keyint",
                    "Keyframe interval (s)",
                    0.0,
                    2.0,
                    1.0,
                    false,
                );
            });
        });
    egui::CollapsingHeader::new("Audio encoding")
        .id_salt("advanced_capture_audio")
        .show(ui, |ui| {
            grid(ui, "audio", |ui| {
                changed |= choice(
                    ui,
                    draft,
                    "audio_codec",
                    "Audio codec",
                    &[("opus", "Opus"), ("aac", "AAC"), ("mp3", "MP3")],
                );
                changed |= number(
                    ui,
                    draft,
                    "audio_bitrate",
                    "Audio bitrate (kbps)",
                    0.0,
                    50000.0,
                    256.0,
                    true,
                );
            });
            ui.small("A bitrate of 0 lets GSR choose.");
        });
    egui::CollapsingHeader::new("Capture behavior")
        .id_salt("advanced_capture_behavior")
        .show(ui, |ui| {
            grid(ui, "behavior", |ui| {
                for (key, label) in [
                    ("cursor", "Include cursor"),
                    ("fallback_cpu_encoding", "CPU encoding fallback"),
                    ("low_power", "Low power mode"),
                    ("restore_portal_session", "Restore portal session"),
                    ("overclock", "GPU overclocking"),
                    ("exclude_metadata", "Exclude metadata"),
                ] {
                    changed |= boolean(ui, draft, key, label);
                }
            });
        });
    egui::CollapsingHeader::new("Region and paths")
        .id_salt("advanced_capture_region")
        .show(ui, |ui| {
            changed |= json_field(
                ui,
                draft,
                "region",
                "Capture region",
                JsonKind::Region,
                r#"{"width":1920,"height":1080,"x":0,"y":0}"#,
            );
            ui.small(
                "Region dimensions use pixels. This applies when the capture target is a region.",
            );
            ui.add_space(8.0);
            changed |= path_field(
                ui,
                draft,
                "portal_session_token_filepath",
                "Portal session token file",
            );
            ui.add_space(8.0);
            changed |= json_field(
                ui,
                draft,
                "plugins",
                "Plugin paths",
                JsonKind::Array,
                r#"["/absolute/path/plugin.so"]"#,
            );
            ui.small("Use a JSON array of absolute .so paths. Up to 16 plugins.");
        });
    egui::CollapsingHeader::new("Codec options and diagnostics")
        .id_salt("advanced_capture_details")
        .show(ui, |ui| {
            changed |= json_field(
                ui,
                draft,
                "video_options",
                "Video codec options",
                JsonKind::Object,
                r#"{"preset":"fast"}"#,
            );
            ui.add_space(8.0);
            changed |= json_field(
                ui,
                draft,
                "audio_options",
                "Audio codec options",
                JsonKind::Object,
                r#"{"application":"audio"}"#,
            );
            ui.add_space(8.0);
            grid(ui, "diagnostics", |ui| {
                changed |= boolean(ui, draft, "verbose", "Verbose capture log");
                changed |= boolean(ui, draft, "gl_debug", "OpenGL debug log");
            });
        });
    changed
}

fn grid(ui: &mut Ui, id: &str, body: impl FnOnce(&mut Ui)) {
    egui::Grid::new(("advanced_capture_form", id))
        .num_columns(2)
        .spacing([16.0, 7.0])
        .show(ui, body);
}
fn replace(draft: &mut Value, key: &str, value: Option<Value>) -> bool {
    if draft.get(key) == value.as_ref() {
        return false;
    }
    let Some(object) = draft.as_object_mut() else {
        return false;
    };
    if let Some(value) = value {
        object.insert(key.to_owned(), value);
    } else {
        object.remove(key);
    }
    true
}
fn choice(
    ui: &mut Ui,
    draft: &mut Value,
    key: &str,
    label: &str,
    choices: &[(&str, &str)],
) -> bool {
    let caption = ui.label(label);
    let mut selected = draft.get(key).cloned();
    let text = selected
        .as_ref()
        .map(|value| {
            choices
                .iter()
                .find(|(key, _)| value.as_str() == Some(*key))
                .map(|(_, label)| *label)
                .unwrap_or("Unsupported value")
        })
        .unwrap_or("Default");
    egui::ComboBox::from_id_salt(("advanced_capture_choice", key))
        .width(190.0)
        .selected_text(text)
        .show_ui(ui, |ui| {
            ui.selectable_value(&mut selected, None, "Default");
            for (value, label) in choices {
                ui.selectable_value(&mut selected, Some(json!(value)), *label);
            }
        })
        .response
        .labelled_by(caption.id);
    ui.end_row();
    replace(draft, key, selected)
}
fn boolean(ui: &mut Ui, draft: &mut Value, key: &str, label: &str) -> bool {
    let caption = ui.label(label);
    let mut selected = draft.get(key).cloned();
    let text = match selected.as_ref() {
        None => "Default",
        Some(Value::Bool(true)) => "On",
        Some(Value::Bool(false)) => "Off",
        _ => "Unsupported value",
    };
    egui::ComboBox::from_id_salt(("advanced_capture_boolean", key))
        .width(190.0)
        .selected_text(text)
        .show_ui(ui, |ui| {
            ui.selectable_value(&mut selected, None, "Default");
            ui.selectable_value(&mut selected, Some(Value::Bool(true)), "On");
            ui.selectable_value(&mut selected, Some(Value::Bool(false)), "Off");
        })
        .response
        .labelled_by(caption.id);
    ui.end_row();
    replace(draft, key, selected)
}
fn number(
    ui: &mut Ui,
    draft: &mut Value,
    key: &str,
    label: &str,
    min: f64,
    max: f64,
    initial: f64,
    integer: bool,
) -> bool {
    let caption = ui.label(label);
    let before = draft.get(key).cloned();
    let mut custom = before.is_some();
    let mut amount = before.as_ref().and_then(Value::as_f64).unwrap_or(initial);
    let mut edited = false;
    ui.horizontal(|ui| {
        egui::ComboBox::from_id_salt(("advanced_capture_number", key))
            .width(100.0)
            .selected_text(if custom { "Custom" } else { "Default" })
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut custom, false, "Default");
                ui.selectable_value(&mut custom, true, "Custom");
            })
            .response
            .labelled_by(caption.id);
        if custom {
            edited = ui
                .add(
                    egui::DragValue::new(&mut amount)
                        .range(min..=max)
                        .speed(if integer { 1.0 } else { 0.1 })
                        .max_decimals(if integer { 0 } else { 2 }),
                )
                .labelled_by(caption.id)
                .changed();
        }
    });
    ui.end_row();
    if !custom {
        return replace(draft, key, None);
    }
    if before.is_none() || edited {
        return replace(
            draft,
            key,
            Some(if integer {
                json!(amount.round() as i64)
            } else {
                json!(amount)
            }),
        );
    }
    false
}

#[derive(Clone)]
struct TextState {
    text: String,
    source: Option<Value>,
    error: Option<String>,
}
fn text_state(ui: &mut Ui, id: egui::Id, value: Option<&Value>, json: bool) -> TextState {
    let source = value.cloned();
    let mut state = ui
        .data_mut(|data| data.get_temp::<TextState>(id))
        .unwrap_or_else(|| TextState {
            text: String::new(),
            source: None,
            error: None,
        });
    if state.source != source {
        state.text = source
            .as_ref()
            .map(|value| {
                if json {
                    serde_json::to_string_pretty(value).unwrap_or_default()
                } else {
                    value.as_str().unwrap_or_default().to_owned()
                }
            })
            .unwrap_or_default();
        state.source = source;
        state.error = None;
    }
    state
}
fn text_error(ui: &mut Ui, error: &Option<String>) {
    if let Some(error) = error {
        ui.colored_label(ui.visuals().error_fg_color, format!("Not applied: {error}"));
    }
}
fn path_field(ui: &mut Ui, draft: &mut Value, key: &str, label: &str) -> bool {
    let caption = ui.label(label);
    let id = ui.make_persistent_id(("advanced_capture_path", key));
    let mut state = text_state(ui, id, draft.get(key), false);
    let response = ui.add(
        egui::TextEdit::singleline(&mut state.text)
            .desired_width(f32::INFINITY)
            .char_limit(4096)
            .hint_text("Default"),
    );
    response.clone().labelled_by(caption.id);
    let mut changed = false;
    if response.changed() {
        if state
            .text
            .chars()
            .any(|c| c < ' ' || matches!(c, '{' | '}' | ';'))
        {
            state.error =
                Some("Use a file path without control characters, braces or semicolons.".into());
        } else {
            let value = (!state.text.is_empty()).then(|| Value::String(state.text.clone()));
            changed = replace(draft, key, value.clone());
            state.source = value;
            state.error = None;
        }
    }
    text_error(ui, &state.error);
    ui.data_mut(|data| data.insert_temp(id, state));
    changed
}
#[derive(Clone, Copy)]
enum JsonKind {
    Object,
    Array,
    Region,
}
fn parse_detail(text: &str, kind: JsonKind) -> Result<Option<Value>, String> {
    if text.trim().is_empty() {
        return Ok(None);
    }
    let value: Value = serde_json::from_str(text).map_err(|error| {
        format!(
            "Invalid JSON at line {}, column {}: {}",
            error.line(),
            error.column(),
            error
        )
    })?;
    match kind {
        JsonKind::Object if !value.is_object() => return Err("Enter a JSON object.".into()),
        JsonKind::Array
            if !value.is_array()
                || value
                    .as_array()
                    .is_some_and(|items| items.iter().any(|item| !item.is_string())) =>
        {
            return Err("Enter a JSON array of plugin paths.".into());
        }
        JsonKind::Region => {
            let object = value
                .as_object()
                .ok_or("Enter a JSON object with width, height, x and y.")?;
            if object.len() != 4
                || !["width", "height", "x", "y"]
                    .iter()
                    .all(|key| object.contains_key(*key))
            {
                return Err("Include only width, height, x and y.".into());
            }
            for key in ["width", "height", "x", "y"] {
                let amount = object[key]
                    .as_i64()
                    .ok_or("Region dimensions and coordinates must be integers.")?;
                if !(if matches!(key, "width" | "height") {
                    0..=32768
                } else {
                    -131072..=131072
                })
                .contains(&amount)
                {
                    return Err(
                        "Use dimensions from 0 to 32768 and coordinates from −131072 to 131072."
                            .into(),
                    );
                }
            }
        }
        _ => {}
    }
    Ok(Some(value))
}
fn json_field(
    ui: &mut Ui,
    draft: &mut Value,
    key: &str,
    label: &str,
    kind: JsonKind,
    example: &str,
) -> bool {
    let id = ui.make_persistent_id(("advanced_capture_json", key));
    let mut state = text_state(ui, id, draft.get(key), true);
    let mut changed = false;
    ui.horizontal(|ui| {
        ui.label(egui::RichText::new(label).strong());
        if ui.small_button("Use Default").clicked() {
            changed |= replace(draft, key, None);
            state.text.clear();
            state.source = None;
            state.error = None;
        }
    });
    let response = ui.add(
        egui::TextEdit::multiline(&mut state.text)
            .desired_rows(3)
            .desired_width(f32::INFINITY)
            .char_limit(98304)
            .hint_text(example),
    );
    if response.changed() {
        match parse_detail(&state.text, kind) {
            Ok(value) => {
                changed |= replace(draft, key, value.clone());
                state.source = value;
                state.error = None;
            }
            Err(error) => state.error = Some(error),
        }
    }
    if state.source.is_none() && state.error.is_none() {
        ui.small("Default");
    }
    text_error(ui, &state.error);
    ui.data_mut(|data| data.insert_temp(id, state));
    changed
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn idle_forms_preserve_absent_defaults_and_existing_values() {
        let context = egui::Context::default();
        for mut draft in [
            json!({}),
            json!({"encoder":"cpu","keyint":0.5,"cursor":false,"region":{"width":1280,"height":720,"x":-100,"y":0},"plugins":["/opt/example.so"],"extension":{"keep":true}}),
        ] {
            let original = draft.clone();
            for _ in 0..3 {
                let _ = context.run(egui::RawInput::default(), |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| assert!(!show(ui, &mut draft)));
                });
            }
            assert_eq!(draft, original);
        }
    }
    #[test]
    fn defaults_remove_only_requested_field_and_keep_false_distinct() {
        let mut draft = json!({"cursor":false,"encoder":"gpu","unknown":{"keep":true}});
        assert!(!replace(&mut draft, "cursor", Some(json!(false))));
        assert!(replace(&mut draft, "cursor", None));
        assert!(draft.get("cursor").is_none());
        assert_eq!(draft["encoder"], "gpu");
        assert_eq!(draft["unknown"], json!({"keep":true}));
    }
    #[test]
    fn json_details_reject_invalid_input_and_allow_explicit_defaults() {
        assert!(parse_detail("{", JsonKind::Object).is_err());
        assert!(parse_detail("[]", JsonKind::Object).is_err());
        assert!(parse_detail(r#"[1]"#, JsonKind::Array).is_err());
        assert!(parse_detail(r#"{"width":1,"height":1,"x":0}"#, JsonKind::Region).is_err());
        assert!(parse_detail(r#"{"width":1,"height":1,"x":0.5,"y":0}"#, JsonKind::Region).is_err());
        assert!(
            parse_detail(
                r#"{"width":32769,"height":1,"x":0,"y":0}"#,
                JsonKind::Region
            )
            .is_err()
        );
        assert_eq!(parse_detail("  ", JsonKind::Object).unwrap(), None);
        assert_eq!(
            parse_detail(
                r#"{"width":0,"height":0,"x":-131072,"y":131072}"#,
                JsonKind::Region
            )
            .unwrap(),
            Some(json!({"width":0,"height":0,"x":-131072,"y":131072}))
        );
    }
    #[test]
    fn invalid_json_text_persists_without_mutation_and_reload_resets_it() {
        let context = egui::Context::default();
        let mut draft = json!({"video_options":{"preset":"fast"}});
        let original = draft.clone();
        for frame in 0..3 {
            let _ = context.run(egui::RawInput::default(), |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    let id = ui.make_persistent_id(("advanced_capture_json", "video_options"));
                    if frame == 0 {
                        ui.data_mut(|data| {
                            data.insert_temp(
                                id,
                                TextState {
                                    text: "{invalid".into(),
                                    source: draft.get("video_options").cloned(),
                                    error: Some("Invalid JSON".into()),
                                },
                            )
                        });
                    }
                    assert!(!json_field(
                        ui,
                        &mut draft,
                        "video_options",
                        "Video codec options",
                        JsonKind::Object,
                        "{}"
                    ));
                    let state = ui.data_mut(|data| data.get_temp::<TextState>(id)).unwrap();
                    assert_eq!(state.text, "{invalid");
                    assert!(state.error.is_some());
                });
            });
        }
        assert_eq!(draft, original);
        draft["video_options"] = json!({"preset":"slow"});
        let _ = context.run(egui::RawInput::default(), |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| {
                let id = ui.make_persistent_id(("advanced_capture_json", "video_options"));
                assert!(!json_field(
                    ui,
                    &mut draft,
                    "video_options",
                    "Video codec options",
                    JsonKind::Object,
                    "{}"
                ));
                let state = ui.data_mut(|data| data.get_temp::<TextState>(id)).unwrap();
                assert!(state.text.contains("slow"));
                assert!(state.error.is_none());
            });
        });
    }
}
