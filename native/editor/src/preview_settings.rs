// SPDX-License-Identifier: GPL-3.0-only
//! Validated preview preferences. Capture and export settings are independent.
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resolution {
    Native,
    FullHd,
    QuadHd,
    UltraHd,
    Custom([u32; 2]),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameRate {
    Source,
    Fps60,
    Fps120,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PreviewSettings {
    pub resolution: Resolution,
    pub frame_rate: FrameRate,
}
impl Default for PreviewSettings {
    fn default() -> Self {
        Self {
            resolution: Resolution::Native,
            frame_rate: FrameRate::Source,
        }
    }
}
impl PreviewSettings {
    pub fn from_config(config: &Value) -> Result<Self, String> {
        if !config.is_object() {
            return Err("Settings must be a JSON object.".into());
        }
        let resolution = match config.get("preview_resolution") {
            None => Resolution::Native,
            Some(Value::String(value)) => match value.as_str() {
                "native" => Resolution::Native,
                "1080p" => Resolution::FullHd,
                "1440p" => Resolution::QuadHd,
                "4k" => Resolution::UltraHd,
                _ => return Err(resolution_error()),
            },
            Some(Value::Object(value)) if value.len() == 2 => {
                let dimension = |name| {
                    value
                        .get(name)
                        .and_then(Value::as_u64)
                        .filter(|v| (32..=32768).contains(v) && v % 2 == 0)
                        .map(|v| v as u32)
                };
                match (dimension("width"), dimension("height")) {
                    (Some(w), Some(h)) => Resolution::Custom([w, h]),
                    _ => return Err(resolution_error()),
                }
            }
            _ => return Err(resolution_error()),
        };
        let frame_rate = match config.get("preview_fps") {
            None => FrameRate::Source,
            Some(Value::String(value)) if value == "source" => FrameRate::Source,
            Some(value) if value.as_u64() == Some(60) => FrameRate::Fps60,
            Some(value) if value.as_u64() == Some(120) => FrameRate::Fps120,
            _ => return Err("preview_fps must be source, 60 or 120.".into()),
        };
        Ok(Self {
            resolution,
            frame_rate,
        })
    }

    /// Maximum raster size; never stretch or upscale a smaller canvas.
    /// Every mode, including Native and Custom, is bounded to 3840 x 2160.
    /// A degenerate canvas produces the smallest supported 2 x 2 placeholder.
    pub fn size(self, canvas: [u32; 2]) -> [u32; 2] {
        let source = [canvas[0].max(2), canvas[1].max(2)];
        let bounds = match self.resolution {
            Resolution::Native => [3840, 2160],
            Resolution::FullHd => [1920, 1080],
            Resolution::QuadHd => [2560, 1440],
            Resolution::UltraHd => [3840, 2160],
            Resolution::Custom(value) => value,
        };
        let scale = (bounds[0] as f64 / source[0] as f64)
            .min(bounds[1] as f64 / source[1] as f64)
            .min(3840.0 / source[0] as f64)
            .min(2160.0 / source[1] as f64)
            .min(1.0);
        source.map(|dimension| ((dimension as f64 * scale) as u32 / 2 * 2).max(2))
    }

    /// Playback/display cadence, never a recording or export FPS mutation.
    /// Explicit rates can repeat source frames; no interpolation is implied.
    pub fn fps(self, model: &Value, project: &Value) -> u32 {
        match self.frame_rate {
            FrameRate::Fps60 => 60,
            FrameRate::Fps120 => 120,
            FrameRate::Source => source_fps(model, project),
        }
    }

    /// Update only these two keys in the caller's existing settings object.
    pub fn write_config(self, config: &mut Value) -> Result<(), String> {
        if let Resolution::Custom(dimensions) = self.resolution {
            if dimensions
                .iter()
                .any(|v| !(32..=32768).contains(v) || v % 2 != 0)
            {
                return Err(resolution_error());
            }
        }
        let object = config
            .as_object_mut()
            .ok_or("Settings must be a JSON object.")?;
        object.insert(
            "preview_resolution".into(),
            match self.resolution {
                Resolution::Native => json!("native"),
                Resolution::FullHd => json!("1080p"),
                Resolution::QuadHd => json!("1440p"),
                Resolution::UltraHd => json!("4k"),
                Resolution::Custom([width, height]) => json!({"width":width,"height":height}),
            },
        );
        object.insert(
            "preview_fps".into(),
            match self.frame_rate {
                FrameRate::Source => json!("source"),
                FrameRate::Fps60 => json!(60),
                FrameRate::Fps120 => json!(120),
            },
        );
        Ok(())
    }
}
fn resolution_error() -> String {
    "preview_resolution must be native, 1080p, 1440p, 4k, or an object with even width and height from 32 to 32768.".into()
}
fn rate(value: &Value) -> Option<f64> {
    let parsed = value.as_f64().or_else(|| {
        let text = value.as_str()?;
        if let Some((numerator, denominator)) = text.split_once('/') {
            Some(numerator.parse::<f64>().ok()? / denominator.parse::<f64>().ok()?)
        } else {
            text.parse::<f64>().ok()
        }
    })?;
    (parsed.is_finite() && (1.0..=240.0).contains(&parsed)).then_some(parsed)
}
fn source_fps(model: &Value, project: &Value) -> u32 {
    let source_rate = project["sources"].as_array().and_then(|sources| {
        sources
            .iter()
            .filter(|source| source["type"] == "video" && source["enabled"] != false)
            .filter_map(|source| {
                ["fps", "avg_frame_rate", "r_frame_rate"]
                    .iter()
                    .find_map(|key| rate(&source[*key]))
            })
            .max_by(f64::total_cmp)
    });
    source_rate
        .or_else(|| rate(&project["fps"]))
        .or_else(|| rate(&model["fps"]))
        .unwrap_or(60.0)
        .round()
        .clamp(1.0, 120.0) as u32
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_and_roundtrip_preserve_capture_preferences() {
        let original =
            json!({"fps":60,"capture_resolution":"1080p","export_quality":20,"custom":"keep"});
        let mut config = original.clone();
        let settings = PreviewSettings::from_config(&config).unwrap();
        assert_eq!(settings, PreviewSettings::default());
        settings.write_config(&mut config).unwrap();
        assert_eq!(PreviewSettings::from_config(&config).unwrap(), settings);
        for key in ["fps", "capture_resolution", "export_quality", "custom"] {
            assert_eq!(config[key], original[key]);
        }
    }
    #[test]
    fn fitting_keeps_aspect_bounds_and_source_detail() {
        let settings = PreviewSettings {
            resolution: Resolution::FullHd,
            ..PreviewSettings::default()
        };
        for (source, expected) in [
            ([1920, 1080], [1920, 1080]),
            ([1280, 720], [1280, 720]),
            ([2560, 1600], [1728, 1080]),
            ([1080, 1920], [606, 1080]),
            ([3440, 1440], [1920, 802]),
            ([0, 0], [2, 2]),
        ] {
            assert_eq!(settings.size(source), expected);
        }
        let native = PreviewSettings {
            resolution: Resolution::Native,
            ..settings
        };
        assert_eq!(native.size([3840, 2160]), [3840, 2160]);
        assert_eq!(native.size([1279, 719]), [1278, 718]);
        assert_eq!(native.size([3840, 1080]), [3840, 1080]);
        assert_eq!(native.size([7680, 2160]), [3840, 1080]);
    }
    #[test]
    fn rejects_malformed_preferences() {
        for resolution in [
            json!(null),
            json!(true),
            json!("720p"),
            json!({"width":1921,"height":1080}),
            json!({"width":1920,"height":1080,"extra":1}),
            json!({"width":0,"height":1080}),
        ] {
            assert!(
                PreviewSettings::from_config(&json!({"preview_resolution":resolution})).is_err()
            );
        }
        for fps in [
            json!(true),
            json!("60"),
            json!(60.0),
            json!(24),
            json!(null),
        ] {
            assert!(PreviewSettings::from_config(&json!({"preview_fps":fps})).is_err());
        }
    }
    #[test]
    fn source_cadence_preserves_60_and_120_without_changing_capture() {
        let settings = PreviewSettings::default();
        assert_eq!(settings.fps(&json!({"fps":24}), &json!({"fps":60})), 60);
        assert_eq!(settings.fps(&json!({"fps":60}), &json!({"fps":120})), 120);
        assert_eq!(
            settings.fps(
                &json!({}),
                &json!({"fps":120,"sources":[{"type":"video","fps":60}]})
            ),
            60
        );
        assert_eq!(
            settings.fps(
                &json!({}),
                &json!({"sources":[{"type":"video","avg_frame_rate":"120000/1001"}]})
            ),
            120
        );
        assert_eq!(settings.fps(&json!({}), &json!({"sources":[{"type":"audio","fps":240},{"type":"video","fps":120,"enabled":false}],"fps":60})),60);
        assert_eq!(settings.fps(&json!({"fps":30}), &json!({"fps":"0/0"})), 30);
        assert_eq!(settings.fps(&json!({}), &json!({})), 60);
        assert_eq!(settings.fps(&json!({}), &json!({"fps":240})), 120);
        assert_eq!(
            PreviewSettings {
                frame_rate: FrameRate::Fps120,
                ..settings
            }
            .fps(&json!({}), &json!({"fps":60})),
            120
        );
    }
}
