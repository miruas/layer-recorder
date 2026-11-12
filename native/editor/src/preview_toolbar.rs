// SPDX-License-Identifier: GPL-3.0-only
//! Inline preview preferences; the caller owns persistence and renderer updates.
use crate::preview_settings::{FrameRate, PreviewSettings, Resolution};
use eframe::egui::{self, WidgetInfo, WidgetType};

/// Returns true only when a preference changed. Place in a wrapping play row.
/// `fps` is the resolved playback cadence, including the source-rate choice.
pub fn show(ui: &mut egui::Ui, settings: &mut PreviewSettings, canvas: [u32; 2], fps: u32) -> bool {
    let before = *settings;
    ui.label("Preview");

    let selected = resolution_label(settings.resolution);
    let resolution = egui::ComboBox::from_id_salt("preview_resolution")
        .width(if matches!(settings.resolution, Resolution::Custom(_)) {
            164.
        } else {
            96.
        })
        .selected_text(&selected)
        .show_ui(ui, |ui| {
            for (value, label) in [
                (Resolution::Native, "Native"),
                (Resolution::FullHd, "1080p max"),
                (Resolution::QuadHd, "1440p max"),
                (Resolution::UltraHd, "4K max"),
            ] {
                ui.selectable_value(&mut settings.resolution, value, label);
            }
            if let Resolution::Custom(_) = settings.resolution {
                ui.separator();
                ui.label(&selected);
            }
        })
        .response;
    resolution.widget_info(|| {
        WidgetInfo::labeled(
            WidgetType::ComboBox,
            ui.is_enabled(),
            format!("Preview resolution: {selected}"),
        )
    });
    resolution.on_hover_text("Resolution limit for the combined preview canvas. Native keeps source detail within the 3840 x 2160 preview limit. Presets keep aspect ratio and never upscale. Recording and export quality are unchanged.");

    let cadence = match settings.frame_rate {
        FrameRate::Source => format!("Source {fps} FPS"),
        FrameRate::Fps60 => "60 FPS".into(),
        FrameRate::Fps120 => "120 FPS".into(),
    };
    let frame_rate = egui::ComboBox::from_id_salt("preview_fps")
        .width(120.)
        .selected_text(&cadence)
        .show_ui(ui, |ui| {
            ui.selectable_value(
                &mut settings.frame_rate,
                FrameRate::Source,
                "Source frame rate",
            );
            ui.selectable_value(&mut settings.frame_rate, FrameRate::Fps60, "60 FPS");
            ui.selectable_value(&mut settings.frame_rate, FrameRate::Fps120, "120 FPS");
        })
        .response;
    frame_rate.widget_info(|| {
        WidgetInfo::labeled(
            WidgetType::ComboBox,
            ui.is_enabled(),
            format!("Preview frame rate: {cadence}"),
        )
    });
    frame_rate.on_hover_text("Target preview cadence. Source follows available source frame-rate metadata, up to 120 FPS. Selecting a faster rate can repeat source frames; no motion interpolation is applied. Capture and export frame rates stay unchanged.");

    // The cadence argument describes the renderer before this row changed it.
    // Explicit selections can update immediately; source resolves next frame.
    let target_fps = match settings.frame_rate {
        FrameRate::Source => fps,
        FrameRate::Fps60 => 60,
        FrameRate::Fps120 => 120,
    };
    let [width, height] = settings.size(canvas);
    ui.label(format!("{width} x {height} at {target_fps} FPS"))
        .on_hover_text("Requested preview size and target cadence, not a measured playback speed. Performance depends on the source codec, effects and hardware. A 60 FPS recording contains 60 source frames per second even when preview targets 120 FPS.");
    *settings != before
}

fn resolution_label(resolution: Resolution) -> String {
    match resolution {
        Resolution::Native => "Native".into(),
        Resolution::FullHd => "1080p max".into(),
        Resolution::QuadHd => "1440p max".into(),
        Resolution::UltraHd => "4K max".into(),
        Resolution::Custom([width, height]) => format!("Custom {width} x {height}"),
    }
}
