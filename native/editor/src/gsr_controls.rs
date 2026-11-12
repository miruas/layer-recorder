// SPDX-License-Identifier: GPL-3.0-only
//! Compact capture controls for the GPU Screen Recorder-owned capture surface.
//!
//! Layer archives and edits independent-source recordings. Every control here
//! maps to a canonical GSR action; its bridge maintains the separate raw tracks.
use super::{button, timecode};
use eframe::egui::{self, Color32, RichText, Vec2};
use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    ToggleRecord,
    ToggleReplay,
    SaveShort,
    SaveLong,
    TogglePause,
    ToggleStream,
    ToggleRegion,
    Screenshot,
    ScreenshotRegion,
    ScreenshotWindow,
    OpenGsr,
    GsrSettings,
    Status,
}

impl Action {
    pub fn command(self) -> &'static str {
        match self {
            Self::ToggleRecord => "toggle-record",
            Self::ToggleReplay => "toggle-replay",
            Self::SaveShort => "replay-save-1-min",
            Self::SaveLong => "replay-save-10-min",
            Self::TogglePause => "toggle-pause",
            Self::ToggleStream => "toggle-stream",
            Self::ToggleRegion => "toggle-record-region",
            Self::Screenshot => "take-screenshot",
            Self::ScreenshotRegion => "take-screenshot-region",
            Self::ScreenshotWindow => "take-screenshot-window",
            Self::OpenGsr => "toggle-show",
            Self::GsrSettings => "show-recording-settings",
            Self::Status => "status",
        }
    }

    pub fn is_replay_save(self) -> bool {
        matches!(self, Self::SaveShort | Self::SaveLong)
    }
}

/// A read-only view of the Layer mirror of GSR's capture state.
#[derive(Clone, Copy, Default)]
pub struct Snapshot {
    pub recording: bool,
    pub replay: bool,
    pub paused: bool,
    pub can_pause: bool,
    pub record_seconds: f64,
    pub buffer_seconds: f64,
}

impl Snapshot {
    pub fn from_status(status: &Value) -> Self {
        let seconds = |key: &str| status[key].as_f64().unwrap_or(0.).max(0.);
        Self {
            recording: status["recording"].as_bool().unwrap_or(false),
            replay: status["replay"].as_bool().unwrap_or(false),
            paused: status["paused"].as_bool().unwrap_or(false),
            can_pause: status["gsr_mode"]
                .as_str()
                .map(|v| v == "record")
                .unwrap_or(status["recording"] == true && status["replay"] != true),
            record_seconds: seconds("record_seconds"),
            buffer_seconds: seconds("buffer_seconds"),
        }
    }

    pub fn summary(self, compact: bool) -> String {
        let mut parts = Vec::new();
        if self.recording {
            let label = if self.paused {
                if compact {
                    "Paused"
                } else {
                    "Recording paused"
                }
            } else if compact {
                "Rec"
            } else {
                "Recording"
            };
            parts.push(format!("{label} {}", timecode(self.record_seconds)));
        }
        if self.replay {
            if self.buffer_seconds > 0.5 {
                let label = if compact { "Replay" } else { "Replay buffered" };
                parts.push(format!("{label} {}", timecode(self.buffer_seconds)));
            } else {
                parts.push("Replay on".into());
            }
        }
        if parts.is_empty() {
            "Capture idle".into()
        } else {
            parts.join(" · ")
        }
    }

    pub fn tone(self, ui: &egui::Ui) -> Color32 {
        if self.recording && !self.paused {
            ui.visuals().error_fg_color
        } else if self.recording || self.replay {
            ui.visuals().weak_text_color()
        } else {
            ui.visuals().weak_text_color()
        }
    }
}

/// Record/replay group. Pause is only offered while a regular recording exists.
pub fn controls(ui: &mut egui::Ui, state: &Snapshot, durations: [u64; 2]) -> Option<Action> {
    let mut action = None;

    let record_label = if state.recording {
        "Stop recording"
    } else {
        "Record"
    };
    let record = button(ui, record_label, state.recording)
        .on_hover_text("Start or stop recording in GPU Screen Recorder.");
    if record.clicked() {
        action = Some(Action::ToggleRecord);
    }

    let replay_label = if state.replay {
        "Stop replay"
    } else {
        "Replay"
    };
    let replay = button(ui, replay_label, false)
        .on_hover_text("Start or stop the GPU Screen Recorder replay buffer.");
    if replay.clicked() {
        action = Some(Action::ToggleReplay);
    }

    if state.replay {
        let replay_label = |seconds: u64| {
            if seconds % 60 == 0 {
                format!("Save {} min", seconds / 60)
            } else {
                format!("Save {seconds} s")
            }
        };
        let short = ui
            .add_enabled(
                state.replay,
                egui::Button::new(replay_label(durations[0])).min_size(Vec2::new(0., 32.)),
            )
            .on_hover_text("Save the configured short replay from GPU Screen Recorder.")
            .on_disabled_hover_text("Enable the replay buffer first.");
        if short.clicked() {
            action = Some(Action::SaveShort);
        }

        let long = ui
            .add_enabled(
                state.replay,
                egui::Button::new(replay_label(durations[1])).min_size(Vec2::new(0., 32.)),
            )
            .on_hover_text("Save the configured long replay from GPU Screen Recorder.")
            .on_disabled_hover_text("Enable the replay buffer first.");
        if long.clicked() {
            action = Some(Action::SaveLong);
        }
    }

    if state.recording {
        let pause_label = if state.recording && state.paused {
            "Resume"
        } else {
            "Pause"
        };
        let pause = ui
        .add_enabled(
            state.recording && state.can_pause,
            egui::Button::new(pause_label).min_size(Vec2::new(0., 32.)),
        )
        .on_hover_text("Pause or resume recording. Replay keeps buffering; Layer keeps original source media and omits paused time from the edited recording.")
        .on_disabled_hover_text("Available for regular recording. GSR cannot pause a recording inside replay or streaming.");
        if pause.clicked() {
            action = Some(Action::TogglePause);
        }
    }

    action
}

/// Explicit advanced capture menu. No control here silently drives Layer-only capture.
pub fn capture_menu(ui: &mut egui::Ui) -> Option<Action> {
    let mut action = None;
    ui.menu_button("Capture", |ui| {
        ui.set_min_width(286.);
        if ui
            .button("Start or stop live broadcast")
            .on_hover_text("Streaming is owned by GPU Screen Recorder.")
            .clicked()
        {
            action = Some(Action::ToggleStream);
            ui.close();
        }
        if ui
            .button("Record a region")
            .on_hover_text("Choose a region in GSR. Layer keeps the full sources so you can change the crop later.")
            .clicked()
        {
            action = Some(Action::ToggleRegion);
            ui.close();
        }
        ui.separator();
        if ui
            .button("Screenshot")
            .on_hover_text("Take a screenshot with GPU Screen Recorder.")
            .clicked()
        {
            action = Some(Action::Screenshot);
            ui.close();
        }
        if ui
            .button("Screenshot a region")
            .on_hover_text("Capture a screen region with GPU Screen Recorder.")
            .clicked()
        {
            action = Some(Action::ScreenshotRegion);
            ui.close();
        }
        if ui
            .button("Screenshot a window")
            .on_hover_text("Capture a window with GPU Screen Recorder.")
            .clicked()
        {
            action = Some(Action::ScreenshotWindow);
            ui.close();
        }
        ui.separator();
        if ui
            .button("Open GSR")
            .on_hover_text("Show or hide the GPU Screen Recorder overlay.")
            .clicked()
        {
            action = Some(Action::OpenGsr);
            ui.close();
        }
        if ui
            .button("GSR settings")
            .on_hover_text("Open GPU Screen Recorder's recording settings.")
            .clicked()
        {
            action = Some(Action::GsrSettings);
            ui.close();
        }
        if ui.button("Refresh capture status").clicked() {
            action = Some(Action::Status);
            ui.close();
        }
        ui.separator();
        ui.label(
            RichText::new("Capture controls use GPU Screen Recorder. Tracks remain separately editable.")
                .small()
                .color(ui.visuals().weak_text_color()),
        );
    });
    action
}

pub fn hint(ui: &mut egui::Ui) {
    ui.label(
        RichText::new(
            "Capture controls use GPU Screen Recorder. Tracks remain separately editable.",
        )
        .small()
        .color(ui.visuals().weak_text_color()),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paused_recording_is_labelled_as_paused() {
        let state = Snapshot {
            recording: true,
            paused: true,
            record_seconds: 83.,
            ..Default::default()
        };
        assert_eq!(state.summary(false), "Recording paused 1:23");
        assert_eq!(state.summary(true), "Paused 1:23");
    }

    #[test]
    fn replay_metrics_come_from_the_buffer_mirror() {
        let state = Snapshot {
            replay: true,
            buffer_seconds: 165.,
            ..Default::default()
        };
        assert_eq!(state.summary(false), "Replay buffered 2:45");
        assert_eq!(state.summary(true), "Replay 2:45");
    }

    #[test]
    fn capture_actions_match_the_canonical_gsr_vocabulary() {
        assert_eq!(Action::ToggleRecord.command(), "toggle-record");
        assert_eq!(Action::ToggleReplay.command(), "toggle-replay");
        assert_eq!(Action::SaveShort.command(), "replay-save-1-min");
        assert_eq!(Action::SaveLong.command(), "replay-save-10-min");
        assert_eq!(Action::TogglePause.command(), "toggle-pause");
        assert_eq!(Action::ToggleStream.command(), "toggle-stream");
        assert_eq!(Action::ToggleRegion.command(), "toggle-record-region");
        assert_eq!(Action::Screenshot.command(), "take-screenshot");
        assert_eq!(Action::ScreenshotRegion.command(), "take-screenshot-region");
        assert_eq!(Action::ScreenshotWindow.command(), "take-screenshot-window");
        assert_eq!(Action::OpenGsr.command(), "toggle-show");
        assert_eq!(Action::GsrSettings.command(), "show-recording-settings");
    }
}
