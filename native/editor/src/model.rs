// SPDX-License-Identifier: GPL-3.0-only
//! Native timeline document model.
//!
//! This is a direct port of the timing, keyframe, slicing and project
//! projection algorithms in `katman/studio_model.py`. JSON is deliberately
//! kept as [`serde_json::Value`] so fields written by other Layer versions
//! survive an edit/save cycle unchanged.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use serde_json::{Map, Value, json};
use tempfile::NamedTempFile;

const HISTORY_LIMIT: usize = 60;
const EPSILON: f64 = 0.000001;

/// A random identifier in the same shape as Python's `uuid.uuid4().hex[:12]`.
pub fn uid() -> String {
    let mut value = uuid::Uuid::new_v4().simple().to_string();
    value.truncate(12);
    value
}

fn as_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        // Some legacy/foreign writers used strings for numeric fields. Python's
        // float() accepted those, so accepting them keeps project loading
        // forgiving without changing normal numeric behaviour.
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }
}

fn num_at(value: &Value, key: &str, default: f64) -> f64 {
    value.get(key).and_then(as_f64).unwrap_or(default)
}

fn number(value: f64) -> Value {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn set_number(target: &mut Value, key: &str, value: f64) {
    if let Some(object) = target.as_object_mut() {
        object.insert(key.to_owned(), number(value));
    }
}

fn set_string(target: &mut Value, key: &str, value: &str) {
    if let Some(object) = target.as_object_mut() {
        object.insert(key.to_owned(), Value::String(value.to_owned()));
    }
}

fn set_value(target: &mut Value, key: &str, value: Value) {
    if let Some(object) = target.as_object_mut() {
        object.insert(key.to_owned(), value);
    }
}

fn text_at(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn raw_pairs(value: Option<&Value>) -> Vec<(f64, f64)> {
    let mut points = Vec::new();
    if let Some(Value::Array(items)) = value {
        for item in items {
            if let Value::Array(pair) = item
                && pair.len() >= 2
                && let (Some(time), Some(amount)) = (as_f64(&pair[0]), as_f64(&pair[1]))
            {
                points.push((time, amount));
            }
        }
    }
    points
}

fn sorted_pairs(value: Option<&Value>) -> Vec<(f64, f64)> {
    let mut points = raw_pairs(value);
    points.sort_by(|left, right| left.partial_cmp(right).unwrap_or(Ordering::Equal));
    points
}

fn key_pairs(item: &Value, key: &str) -> Vec<(f64, f64)> {
    sorted_pairs(item.get("keys").and_then(|keys| keys.get(key)))
}

/// Evaluate an animated property at `time`, using the same boundary and
/// interpolation rules as `studio_model.value_at`.
pub fn value_at(item: &Value, key: &str, time: f64, default: f64) -> f64 {
    let points = key_pairs(item, key);
    if points.is_empty() {
        return item.get(key).and_then(as_f64).unwrap_or(default);
    }
    if time <= points[0].0 {
        return points[0].1;
    }
    for window in points.windows(2) {
        let (left_time, left_value) = window[0];
        let (right_time, right_value) = window[1];
        if left_time <= time && time <= right_time {
            let factor = (time - left_time) / (right_time - left_time).max(EPSILON);
            return left_value + (right_value - left_value) * factor;
        }
    }
    points[points.len() - 1].1
}

#[derive(Clone, Copy, Debug)]
struct RateSegment {
    start: f64,
    duration: f64,
    offset: f64,
    rate: f64,
}

fn base_length(clip: &Value) -> f64 {
    clip.get("length")
        .and_then(as_f64)
        .unwrap_or_else(|| num_at(clip, "duration", 1.0) * num_at(clip, "speed", 1.0))
}

fn rate_segments(clip: &Value) -> Vec<RateSegment> {
    let length = base_length(clip);
    let mut keys: Vec<(f64, f64)> = raw_pairs(clip.get("speed_keys"))
        .into_iter()
        .map(|(time, rate)| (time.clamp(0.0, length), rate))
        .collect();
    keys.sort_by(|left, right| left.partial_cmp(right).unwrap_or(Ordering::Equal));
    if keys.is_empty() || keys[0].0 > 0.0 {
        keys.insert(0, (0.0, num_at(clip, "speed", 1.0)));
    }

    let mut pieces = Vec::new();
    let mut output = 0.0;
    for index in 0..keys.len() {
        let (start, rate) = keys[index];
        let end = keys.get(index + 1).map(|(time, _)| *time).unwrap_or(length);
        if end <= start || !rate.is_finite() || rate <= 0.0 {
            continue;
        }
        pieces.push(RateSegment {
            start: output,
            duration: (end - start) / rate,
            offset: start,
            rate,
        });
        output += (end - start) / rate;
    }
    pieces
}

/// Timeline duration of a clip after variable speed and reverse/freeze rules.
pub fn clip_duration(clip: &Value) -> f64 {
    if clip.get("freeze").is_some_and(truthy) {
        return num_at(clip, "duration", 1.0);
    }
    rate_segments(clip)
        .iter()
        .map(|segment| segment.duration)
        .sum()
}

/// Source-media position used for a local clip time, matching the Python model.
pub fn source_offset(clip: &Value, time: f64) -> f64 {
    if clip.get("freeze").is_some_and(truthy) {
        let fallback = num_at(clip, "in", 0.0);
        return clip.get("freeze_at").and_then(as_f64).unwrap_or(fallback);
    }
    let total = clip_duration(clip);
    let time = time.clamp(0.0, total);
    for segment in rate_segments(clip) {
        if time <= segment.start + segment.duration + EPSILON {
            let mut local = segment.offset + (time - segment.start) * segment.rate;
            if clip.get("reverse").is_some_and(truthy) {
                local = base_length(clip) - local;
            }
            return num_at(clip, "in", 0.0) + local;
        }
    }
    num_at(clip, "in", 0.0) + num_at(clip, "length", 0.0)
}

/// Total timeline duration including clips, annotations and subtitles.
pub fn duration(model: &Value) -> f64 {
    let mut maximum = 0.0_f64;
    if let Some(tracks) = model.get("tracks").and_then(Value::as_array) {
        for track in tracks {
            if let Some(clips) = track.get("clips").and_then(Value::as_array) {
                for clip in clips {
                    maximum = maximum.max(num_at(clip, "start", 0.0) + clip_duration(clip));
                }
            }
        }
    }
    for field in ["annotations", "subtitles"] {
        if let Some(items) = model.get(field).and_then(Value::as_array) {
            for item in items {
                maximum = maximum.max(num_at(item, "end", 0.0));
            }
        }
    }
    maximum
}

fn slice_keys(item: &Value, start: f64, end: f64) -> Value {
    let mut result = Map::new();
    if let Some(keys) = item.get("keys").and_then(Value::as_object) {
        for (key, raw) in keys {
            let points = sorted_pairs(Some(raw));
            let mut rows: Vec<Value> = Vec::new();
            rows.push(json!([0.0, value_at(item, key, start, 0.0)]));
            for (time, amount) in points {
                if start < time && time < end {
                    rows.push(json!([time - start, amount]));
                }
            }
            rows.push(json!([end - start, value_at(item, key, end, 0.0)]));
            result.insert(key.clone(), Value::Array(rows));
        }
    }
    Value::Object(result)
}

fn slice_clip(clip: &Value, start: f64, end: f64) -> Value {
    let length = base_length(clip);
    let consumed = |time: f64| -> f64 {
        for segment in rate_segments(clip) {
            if time <= segment.start + segment.duration + 0.00001 {
                return segment.offset + (time - segment.start).max(0.0) * segment.rate;
            }
        }
        length
    };
    let first = consumed(start);
    let last = consumed(end);
    let amount = (last - first).max(0.001);
    let reverse = clip.get("reverse").is_some_and(truthy);
    let source_in = num_at(clip, "in", 0.0) + if reverse { length - last } else { first };

    let mut result = clip.clone();
    set_number(&mut result, "in", source_in);
    set_number(&mut result, "length", amount);
    set_number(&mut result, "start", num_at(clip, "start", 0.0) + start);

    if clip.get("speed_keys").is_some_and(truthy) {
        let keys = sorted_pairs(clip.get("speed_keys"));
        let mut rate = num_at(clip, "speed", 1.0);
        for (time, value) in &keys {
            if *time <= first + 0.00001 {
                rate = *value;
            }
        }
        let mut sliced = Vec::new();
        sliced.push(json!([0.0, rate]));
        for (time, value) in keys {
            if first < time && time < last {
                sliced.push(json!([time - first, value]));
            }
        }
        set_value(&mut result, "speed_keys", Value::Array(sliced));
    }

    set_number(&mut result, "duration", end - start);
    set_value(&mut result, "keys", slice_keys(clip, start, end));
    set_number(
        &mut result,
        "effect_offset",
        num_at(clip, "effect_offset", 0.0) + start,
    );
    let effect_length = clip
        .get("effect_length")
        .and_then(as_f64)
        .unwrap_or_else(|| clip_duration(clip));
    set_number(&mut result, "effect_length", effect_length);
    result
}

fn trim_clip(clip: &mut Value, left: bool, delta: f64) {
    let total = clip_duration(clip);

    // Removing a duration from an edge is a source-preserving slice. This also
    // slices the evaluated keyframe and fade envelope.
    if (left && delta >= 0.0) || (!left && delta <= 0.0) {
        let (start, end) = if left {
            (delta.min(total - 0.04), total)
        } else {
            (0.0, (total + delta).max(0.04))
        };
        *clip = slice_clip(clip, start, end);
        return;
    }

    if clip.get("freeze").is_some_and(truthy) {
        let mut extra = if left { -delta } else { delta };
        if left {
            extra = extra.min(num_at(clip, "start", 0.0));
            let shifted = num_at(clip, "start", 0.0) - extra;
            set_number(clip, "start", shifted);
        }
        set_number(clip, "duration", total + extra);
        return;
    }

    // Animated rate segments are extended by splitting the first/last segment
    // rather than by reinterpreting the whole ramp.
    if clip.get("speed_keys").is_some_and(truthy) {
        let segments = rate_segments(clip);
        let rate = if left {
            segments.first().map(|segment| segment.rate)
        } else {
            segments.last().map(|segment| segment.rate)
        }
        .unwrap_or_else(|| num_at(clip, "speed", 1.0));
        let mut result = clip.clone();
        if left {
            let source_in = num_at(clip, "in", 0.0);
            let extra = (-delta).min(source_in / rate);
            set_number(
                &mut result,
                "start",
                (num_at(clip, "start", 0.0) - extra).max(0.0),
            );
            set_number(&mut result, "in", source_in - extra * rate);
            set_number(&mut result, "length", base_length(clip) + extra * rate);
            let mut keys = Vec::new();
            keys.push(json!([0.0, rate]));
            for (time, value) in raw_pairs(clip.get("speed_keys")) {
                keys.push(json!([time + extra * rate, value]));
            }
            set_value(&mut result, "speed_keys", Value::Array(keys));
        } else {
            set_number(&mut result, "length", base_length(clip) + delta * rate);
        }
        *clip = result;
        let duration = clip_duration(clip);
        set_number(clip, "duration", duration);
        return;
    }

    let speed = num_at(clip, "speed", 1.0);
    let source_in = num_at(clip, "in", 0.0);
    if left {
        let delta = (-source_in / speed).max((total - 0.04).min(delta));
        set_number(clip, "start", (num_at(clip, "start", 0.0) + delta).max(0.0));
        if !clip.get("reverse").is_some_and(truthy) {
            set_number(clip, "in", source_in + delta * speed);
        }
        set_number(clip, "length", base_length(clip) - delta * speed);
    } else {
        let delta = (0.04 - total).max(delta);
        set_number(clip, "length", base_length(clip) + delta * speed);
        if clip.get("reverse").is_some_and(truthy) {
            set_number(clip, "in", source_in - delta * speed);
        }
    }
    let duration = clip_duration(clip);
    set_number(clip, "duration", duration);
}

fn clip_location(model: &Value, id: &str) -> Option<(usize, usize)> {
    let tracks = model.get("tracks")?.as_array()?;
    for (track_index, track) in tracks.iter().enumerate() {
        let clips = track.get("clips")?.as_array()?;
        for (clip_index, clip) in clips.iter().enumerate() {
            if clip.get("id").and_then(Value::as_str) == Some(id) {
                return Some((track_index, clip_index));
            }
        }
    }
    None
}

fn clip_by_id<'a>(model: &'a Value, id: &str) -> Option<&'a Value> {
    let (track, clip) = clip_location(model, id)?;
    model.get("tracks")?.get(track)?.get("clips")?.get(clip)
}

#[allow(dead_code)]
fn split_clip(model: &mut Value, id: &str, position: f64) -> Result<String> {
    let (track_index, clip_index) =
        clip_location(model, id).ok_or_else(|| anyhow!("Select a timeline clip first."))?;
    let clip = model
        .get("tracks")
        .and_then(|value| value.get(track_index))
        .and_then(|value| value.get("clips"))
        .and_then(|value| value.get(clip_index))
        .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
    let local = position - num_at(clip, "start", 0.0);
    let total = clip_duration(clip);
    if !(local > 0.02 && local < total - 0.02) {
        bail!("Place the playhead inside a selected clip.");
    }

    let first = slice_clip(clip, 0.0, local);
    let mut second = slice_clip(clip, local, total);
    let second_id = uid();
    set_string(&mut second, "id", &second_id);

    let clips = model
        .get_mut("tracks")
        .and_then(|value| value.get_mut(track_index))
        .and_then(|value| value.get_mut("clips"))
        .and_then(Value::as_array_mut)
        .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
    clips.splice(clip_index..clip_index + 1, [first, second]);
    Ok(second_id)
}

fn python_round(value: f64) -> f64 {
    // Python's round() uses round-half-to-even. Subtitle sizes are the only
    // place the reference model rounds.
    let floor = value.floor();
    let fraction = value - floor;
    if fraction < 0.5 {
        floor
    } else if fraction > 0.5 {
        floor + 1.0
    } else if floor % 2.0 == 0.0 {
        floor
    } else {
        floor + 1.0
    }
}

fn even_floor(value: f64) -> f64 {
    // Mirrors Python's `int(value) // 2 * 2` for the non-negative crop values
    // accepted by project validation.
    (value.trunc() / 2.0).floor() * 2.0
}

fn crop_geometry(width: f64, height: f64, section: &Value) -> (f64, f64, f64, f64) {
    let crop = section
        .get("crop")
        .and_then(Value::as_array)
        .filter(|crop| crop.len() >= 4);
    let crop_value = |index: usize, default: f64| {
        crop.and_then(|crop| crop.get(index))
            .and_then(as_f64)
            .unwrap_or(default)
    };
    let x = crop_value(0, 0.0);
    let y = crop_value(1, 0.0);
    let crop_width = crop_value(2, 1.0);
    let crop_height = crop_value(3, 1.0);

    let left = even_floor(width * x);
    let top = even_floor(height * y);
    let result_width = 2.0_f64.max(even_floor(width * crop_width).min(even_floor(width - left)));
    let result_height = 2.0_f64.max(even_floor(height * crop_height).min(even_floor(height - top)));
    (left, top, result_width, result_height)
}

fn source_rect(value: &Value) -> Option<(f64, f64, f64, f64)> {
    let rect = value.as_array()?;
    if rect.len() != 4 {
        return None;
    }
    Some((
        as_f64(&rect[0])?,
        as_f64(&rect[1])?,
        as_f64(&rect[2])?,
        as_f64(&rect[3])?,
    ))
}

type Rect = (f64, f64, f64, f64);

fn layout(videos: &[&Value]) -> (f64, f64, HashMap<String, Rect>) {
    if videos.is_empty() {
        return (1920.0, 1080.0, HashMap::new());
    }
    if videos.len() == 1 {
        let source = videos[0];
        let width = num_at(source, "width", 1920.0);
        let height = num_at(source, "height", 1080.0);
        let mut rects = HashMap::new();
        rects.insert(text_at(source, "id"), (0.0, 0.0, width, height));
        return (width, height, rects);
    }

    let left = videos
        .iter()
        .map(|source| num_at(source, "x", 0.0))
        .fold(f64::INFINITY, f64::min);
    let top = videos
        .iter()
        .map(|source| num_at(source, "y", 0.0))
        .fold(f64::INFINITY, f64::min);
    let mut rects = HashMap::new();
    let mut right = 0.0_f64;
    let mut bottom = 0.0_f64;
    for source in videos {
        let x = num_at(source, "x", 0.0) - left;
        let y = num_at(source, "y", 0.0) - top;
        let width = source
            .get("logical_width")
            .and_then(as_f64)
            .unwrap_or_else(|| num_at(source, "width", 1920.0));
        let height = source
            .get("logical_height")
            .and_then(as_f64)
            .unwrap_or_else(|| num_at(source, "height", 1080.0));
        rects.insert(text_at(source, "id"), (x, y, width, height));
        right = right.max(x + width);
        bottom = bottom.max(y + height);
    }
    (right, bottom, rects)
}

fn layer_position(order: &[String], id: &str) -> usize {
    order
        .iter()
        .position(|candidate| candidate == id)
        .unwrap_or(order.len())
}

fn scene_layout(videos: &[&Value], section: &Value) -> (f64, f64, HashMap<String, Rect>) {
    if videos.is_empty() {
        return (1920.0, 1080.0, HashMap::new());
    }
    let (_, _, defaults) = layout(videos);
    let order: Vec<String> = section
        .get("layer_order")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_else(|| videos.iter().map(|source| text_at(source, "id")).collect());

    let mut ordered: Vec<&Value> = videos.to_vec();
    ordered.sort_by_key(|source| layer_position(&order, &text_at(source, "id")));

    let placements = section.get("placements").and_then(Value::as_object);
    let mut rects = HashMap::new();
    for source in ordered {
        let id = text_at(source, "id");
        let rect = placements
            .and_then(|placements| placements.get(&id))
            .and_then(source_rect)
            .or_else(|| defaults.get(&id).copied())
            .unwrap_or((0.0, 0.0, 1920.0, 1080.0));
        rects.insert(id, rect);
    }

    let left = rects
        .values()
        .map(|rect| rect.0)
        .fold(f64::INFINITY, f64::min);
    let top = rects
        .values()
        .map(|rect| rect.1)
        .fold(f64::INFINITY, f64::min);
    let mut right = 0.0_f64;
    let mut bottom = 0.0_f64;
    for rect in rects.values_mut() {
        rect.0 -= left;
        rect.1 -= top;
        right = right.max(rect.0 + rect.2);
        bottom = bottom.max(rect.1 + rect.3);
    }
    (
        (right / 2.0).ceil() * 2.0,
        (bottom / 2.0).ceil() * 2.0,
        rects,
    )
}

fn project_sections(project: &Value, sources: &[Value]) -> Vec<Value> {
    if let Some(sections) = project.get("sections")
        && truthy(sections)
    {
        return sections.as_array().cloned().unwrap_or_default();
    }

    let enabled_ids: Vec<Value> = sources
        .iter()
        .filter(|source| source.get("enabled").map(truthy).unwrap_or(true))
        .map(|source| Value::String(text_at(source, "id")))
        .collect();
    let mut gains = Map::new();
    for source in sources {
        gains.insert(text_at(source, "id"), number(num_at(source, "gain", 1.0)));
    }
    vec![json!({
        "start": 0.0,
        "end": num_at(project, "duration", 0.0),
        "enabled_ids": enabled_ids,
        "gains": Value::Object(gains),
    })]
}

/// Project a saved Layer recording into an editable studio timeline.
///
/// The reference Python function trusts that section enables refer to existing
/// sources; malformed projects yield an error instead of a partially-built
/// model here.
pub fn from_project(project: &Value) -> Result<Value> {
    if let Some(studio) = project.get("studio")
        && truthy(studio)
    {
        return Ok(studio.clone());
    }

    let sources = project
        .get("sources")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("This project has no source list."))?;
    let sections = project_sections(project, sources);
    if sections.is_empty() {
        bail!("There are no timeline sections in this project.");
    }

    let source_by_id: HashMap<String, &Value> = sources
        .iter()
        .map(|source| (text_at(source, "id"), source))
        .collect();

    let mut sizes = Vec::new();
    for section in &sections {
        let enabled = string_set(section.get("enabled_ids"));
        let videos: Vec<&Value> = sources
            .iter()
            .filter(|source| {
                text_at(source, "type") == "video" && enabled.contains(&text_at(source, "id"))
            })
            .collect();
        let (width, height, _) = scene_layout(&videos, section);
        let (_, _, crop_width, crop_height) = crop_geometry(width, height, section);
        sizes.push((crop_width, crop_height));
    }
    let width = sizes
        .iter()
        .map(|(width, _)| *width)
        .fold(f64::NEG_INFINITY, f64::max);
    let height = sizes
        .iter()
        .map(|(_, height)| *height)
        .fold(f64::NEG_INFINITY, f64::max);

    let mut tracks: Vec<Value> = sources
        .iter()
        .map(|source| {
            json!({
                "id": uid(),
                "source_id": text_at(source, "id"),
                "name": text_at(source, "name"),
                "kind": text_at(source, "type"),
                "mute": false,
                "visible": true,
                "clips": [],
            })
        })
        .collect();

    let mut annotations = Vec::new();
    let mut offset = 0.0_f64;
    for (section_index, section) in sections.iter().enumerate() {
        let speed = num_at(section, "speed", 1.0);
        let start = num_at(section, "start", 0.0);
        let end = num_at(section, "end", 0.0);
        let span = end - start;
        let enabled = string_set(section.get("enabled_ids"));
        let videos: Vec<&Value> = sources
            .iter()
            .filter(|source| {
                text_at(source, "type") == "video" && enabled.contains(&text_at(source, "id"))
            })
            .collect();
        let (scene_width, scene_height, rects) = scene_layout(&videos, section);
        let (crop_x, crop_y, _, _) = crop_geometry(scene_width, scene_height, section);
        let (section_width, section_height) = sizes[section_index];

        for track in &mut tracks {
            let source_id = text_at(track, "source_id");
            if !enabled.contains(&source_id) {
                continue;
            }
            let Some(source) = source_by_id.get(&source_id) else {
                bail!("A selected source is missing from this project.");
            };
            let timeline_start = num_at(source, "timeline_start", 0.0);
            let trim_in = num_at(source, "trim_in", 0.0);
            let delay = (timeline_start - start).max(0.0);
            let local = (start - timeline_start + trim_in).max(0.0);
            let media_duration = source
                .get("media_duration")
                .and_then(as_f64)
                .unwrap_or(span);
            let length = (span - delay).min((media_duration - local).max(0.0));
            if length <= 0.001 {
                continue;
            }

            let mut clip = json!({
                "id": uid(),
                "source_id": source_id,
                "name": text_at(source, "name"),
                "start": offset + delay / speed,
                "in": local,
                "length": length,
                "duration": length / speed,
                "speed": speed,
                "gain": section
                    .get("gains")
                    .and_then(|gains| gains.get(&source_id))
                    .and_then(as_f64)
                    .unwrap_or(1.0),
                "keys": {},
            });

            if text_at(source, "type") == "video" {
                let (x, y, clip_width, clip_height) =
                    rects
                        .get(&source_id)
                        .copied()
                        .unwrap_or((0.0, 0.0, scene_width, scene_height));
                set_number(&mut clip, "x", x - crop_x + (width - section_width) / 2.0);
                set_number(&mut clip, "y", y - crop_y + (height - section_height) / 2.0);
                set_number(&mut clip, "w", clip_width);
                set_number(&mut clip, "h", clip_height);
                set_number(&mut clip, "opacity", 1.0);
                set_number(&mut clip, "rotation", 0.0);
                set_value(
                    &mut clip,
                    "blackout",
                    Value::Bool(
                        section
                            .get("blackout_ids")
                            .map(|value| string_set(Some(value)))
                            .unwrap_or_default()
                            .contains(&source_id),
                    ),
                );
            }

            if let Some(clips) = track.get_mut("clips").and_then(Value::as_array_mut) {
                clips.push(clip);
            }
        }

        if let Some(strokes) = section.get("annotations").and_then(Value::as_array) {
            for stroke in strokes {
                if !stroke.is_object() {
                    continue;
                }
                let mut annotation = stroke.clone();
                set_value(&mut annotation, "id", Value::String(uid()));
                set_value(
                    &mut annotation,
                    "kind",
                    Value::String(text_at(stroke, "tool")),
                );
                set_number(&mut annotation, "start", offset);
                set_number(&mut annotation, "end", offset + span / speed);
                set_number(&mut annotation, "x", (width - section_width) / 2.0);
                set_number(&mut annotation, "y", (height - section_height) / 2.0);
                set_number(&mut annotation, "w", section_width);
                set_number(&mut annotation, "h", section_height);
                annotations.push(annotation);
            }
        }
        offset += span / speed;
    }

    Ok(json!({
        "version": 1,
        "width": width,
        "height": height,
        "fps": num_at(project, "fps", 60.0).trunc() as i64,
        "tracks": tracks,
        "annotations": annotations,
        "subtitles": [],
        "subtitle_style": {
            "font": "Noto Sans",
            "size": python_round(height * 0.045).max(20.0),
            "color": "#ffffff",
        },
        "snap": true,
    }))
}

fn string_set(value: Option<&Value>) -> HashSet<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn validate_clip_bounds(clip: &Value, project: &Value) -> Result<()> {
    let source_id = clip
        .get("source_id")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Check clip timing and source media."))?;
    let sources = project
        .get("sources")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("This project has no source list."))?;
    let source = sources
        .iter()
        .find(|source| source.get("id").and_then(Value::as_str) == Some(source_id))
        .ok_or_else(|| anyhow!("Check clip timing and source media."))?;

    let start = num_at(clip, "start", 0.0);
    let source_in = num_at(clip, "in", 0.0);
    let length = clip.get("length").and_then(as_f64).unwrap_or(0.0);
    let speed = num_at(clip, "speed", 1.0);
    if !start.is_finite()
        || !source_in.is_finite()
        || !length.is_finite()
        || start < 0.0
        || source_in < 0.0
        || length <= 0.0
        || !(0.25..=4.0).contains(&speed)
    {
        bail!("Check clip timing and source media.");
    }

    let crop_x = num_at(clip, "crop_x", 0.0);
    let crop_y = num_at(clip, "crop_y", 0.0);
    let crop_w = num_at(clip, "crop_w", 1.0);
    let crop_h = num_at(clip, "crop_h", 1.0);
    if crop_x < 0.0
        || crop_y < 0.0
        || crop_w <= 0.0
        || crop_h <= 0.0
        || crop_x + crop_w > 1.00001
        || crop_y + crop_h > 1.00001
    {
        bail!("Keep the source crop inside the frame.");
    }

    if !clip.get("freeze").is_some_and(truthy) {
        let media_duration = source
            .get("media_duration")
            .and_then(as_f64)
            .unwrap_or(f64::INFINITY);
        if source_in + length > media_duration + 0.1 {
            bail!("The clip extends beyond its source media.");
        }
    }

    for (time, rate) in raw_pairs(clip.get("speed_keys")) {
        if time < 0.0 || !(0.25..=4.0).contains(&rate) {
            bail!("Speed segment rates must stay between 0.25× and 4×.");
        }
    }

    if let Some(keys) = clip.get("keys").and_then(Value::as_object) {
        for (key, points) in keys {
            for (time, value) in sorted_pairs(Some(points)) {
                if time < 0.0
                    || !value.is_finite()
                    || ((key == "w" || key == "h") && value < 2.0)
                    || (key == "opacity" && !(0.0..=1.0).contains(&value))
                    || (key == "gain" && !(0.0..=4.0).contains(&value))
                {
                    bail!("Invalid keyframe value or range.");
                }
            }
        }
    }
    Ok(())
}

fn expand_tilde(path: &Path) -> PathBuf {
    if let Ok(rest) = path.strip_prefix("~")
        && let Some(home) = env::var_os("HOME")
    {
        return PathBuf::from(home).join(rest);
    }
    path.to_path_buf()
}

fn normalize_path(path: &Path) -> PathBuf {
    let absolute = path.is_absolute();
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => result.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                if !result.pop() && !absolute {
                    result.push("..");
                }
            }
            Component::Normal(part) => result.push(part),
        }
    }
    result
}

fn absolute_path(path: &Path) -> PathBuf {
    if path.is_absolute() {
        normalize_path(path)
    } else {
        normalize_path(
            &env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(path),
        )
    }
}

/// A loaded project plus its independent timeline model and UI-local state.
pub struct Document {
    pub path: PathBuf,
    pub project: Value,
    pub model: Value,
    pub selected: HashSet<String>,
    pub primary: Option<String>,
    pub position: f64,
    pub revision: u64,
    pub clipboard: Vec<Value>,
    undo_stack: Vec<Value>,
    redo_stack: Vec<Value>,
    clipboard_tracks: Vec<String>,
}

impl Document {
    /// Load a project file, or a project directory containing `project.katman`.
    pub fn load(path: &Path) -> Result<Self> {
        let expanded = expand_tilde(path);
        let project_path = if expanded.is_dir() {
            expanded.join("project.katman")
        } else {
            expanded
        };
        let path = fs::canonicalize(&project_path).unwrap_or_else(|_| absolute_path(&project_path));
        let text = fs::read_to_string(&path)
            .with_context(|| format!("Could not read project {}", path.display()))?;
        let project: Value = serde_json::from_str(&text)
            .with_context(|| format!("Could not parse project {}", path.display()))?;
        if project.get("format").and_then(Value::as_str) != Some("katman")
            || project.get("version").and_then(as_f64) != Some(1.0)
        {
            bail!("This project format is not supported.");
        }

        let sources = project
            .get("sources")
            .and_then(Value::as_array)
            .ok_or_else(|| anyhow!("This project has no source list."))?;
        let base = path
            .parent()
            .ok_or_else(|| anyhow!("The project path has no parent folder."))?;
        let base = fs::canonicalize(base).unwrap_or_else(|_| absolute_path(base));
        for source in sources {
            let file = source
                .get("file")
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow!("A project source is missing its media path."))?;
            let media = if Path::new(file).is_absolute() {
                normalize_path(Path::new(file))
            } else {
                normalize_path(&base.join(file))
            };
            let checked = fs::canonicalize(&media).unwrap_or(media);
            if !checked.starts_with(&base) {
                bail!("Media files must be inside the project folder.");
            }
        }

        let model = match project.get("studio") {
            Some(studio) if truthy(studio) => studio.clone(),
            _ => from_project(&project).context("Could not build the timeline model.")?,
        };

        for name in ["width", "height"] {
            let dimension = model[name].as_f64().unwrap_or(0.);
            if !dimension.is_finite() || !(2. ..=16384.).contains(&dimension) {
                bail!("Invalid canvas dimensions. Open an intact recording or restore its timeline metadata.");
            }
        }
        if !model["tracks"].is_array() || !model["annotations"].is_array() || !model["subtitles"].is_array() {
            bail!("The timeline metadata is incomplete.");
        }

        Ok(Self {
            path,
            project,
            model,
            selected: HashSet::new(),
            primary: None,
            position: 0.0,
            revision: 0,
            clipboard: Vec::new(),
            undo_stack: Vec::new(),
            redo_stack: Vec::new(),
            clipboard_tracks: Vec::new(),
        })
    }

    /// Atomically replace only `project.studio`, leaving sources and every
    /// unknown project field exactly as loaded.
    pub fn save(&mut self) -> Result<()> {
        if !self.project.is_object() {
            bail!("The project root must be a JSON object.");
        }
        set_value(&mut self.project, "studio", self.model.clone());
        let parent = self
            .path
            .parent()
            .ok_or_else(|| anyhow!("The project path has no parent folder."))?;
        fs::create_dir_all(parent)
            .with_context(|| format!("Could not create project folder {}", parent.display()))?;
        let bytes = serde_json::to_vec_pretty(&self.project)?;
        let mut temporary = NamedTempFile::new_in(parent).with_context(|| {
            format!("Could not create a temporary file in {}", parent.display())
        })?;
        temporary
            .write_all(&bytes)
            .context("Could not write the project file.")?;
        temporary
            .flush()
            .context("Could not flush the project file.")?;
        temporary
            .as_file()
            .sync_all()
            .context("Could not sync the project file.")?;
        temporary.persist(&self.path).map_err(|error| {
            anyhow!("Could not replace {}: {}", self.path.display(), error.error)
        })?;
        Ok(())
    }

    /// Snapshot the model for undo, keeping the Python model's 60-step limit.
    pub fn checkpoint(&mut self) {
        self.undo_stack.push(self.model.clone());
        if self.undo_stack.len() > HISTORY_LIMIT {
            let excess = self.undo_stack.len() - HISTORY_LIMIT;
            self.undo_stack.drain(0..excess);
        }
        self.redo_stack.clear();
    }

    pub fn undo(&mut self) {
        let Some(previous) = self.undo_stack.pop() else {
            return;
        };
        self.redo_stack.push(self.model.clone());
        self.model = previous;
        self.prune_selection();
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
    }

    pub fn redo(&mut self) {
        let Some(next) = self.redo_stack.pop() else {
            return;
        };
        self.undo_stack.push(self.model.clone());
        if self.undo_stack.len() > HISTORY_LIMIT {
            let excess = self.undo_stack.len() - HISTORY_LIMIT;
            self.undo_stack.drain(0..excess);
        }
        self.model = next;
        self.prune_selection();
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
    }

    pub fn crossfade_selected(&mut self,seconds:f64)->Result<()> {
        let mut clips:Vec<(String,Value)>=self.model["tracks"].as_array().into_iter().flatten().flat_map(|track|track["clips"].as_array().into_iter().flatten().filter(|clip|clip["id"].as_str().is_some_and(|id|self.selected.contains(id))).map(|clip|(track["kind"].as_str().unwrap_or("").to_string(),clip.clone()))).collect();
        if clips.len()!=2||clips[0].0!=clips[1].0{bail!("Select two video clips or two audio clips to crossfade.");}
        clips.sort_by(|a,b|num_at(&a.1,"start",0.).total_cmp(&num_at(&b.1,"start",0.)));
        if !seconds.is_finite()||seconds<0.04||seconds>clip_duration(&clips[0].1).min(clip_duration(&clips[1].1)){bail!("Choose a crossfade shorter than both selected clips.");}
        let a=clips[0].1["id"].clone();let b=clips[1].1["id"].clone();let audio=clips[0].0=="audio";
        let start=num_at(&clips[0].1,"start",0.)+clip_duration(&clips[0].1)-seconds;
        self.checkpoint();
        for track in self.model["tracks"].as_array_mut().unwrap(){for clip in track["clips"].as_array_mut().unwrap(){if clip["id"]==a{clip["fade_out"]=json!(if audio{seconds}else{0.});}if clip["id"]==b{clip["start"]=json!(start);clip["fade_in"]=json!(seconds);}}}
        self.revision+=1;Ok(())
    }

    pub fn selected_clip(&self) -> Option<&Value> {
        let id = self.selected_id()?;
        clip_by_id(&self.model, &id)
    }

    pub fn selected_clip_mut(&mut self) -> Option<&mut Value> {
        let id = self.selected_id()?;
        let (track, clip) = clip_location(&self.model, &id)?;
        self.model
            .get_mut("tracks")?
            .get_mut(track)?
            .get_mut("clips")?
            .get_mut(clip)
    }

    /// Split every selected clip at the current playhead. The validation pass
    /// completes before checkpointing, so a failed split leaves no history.
    pub fn split_selected(&mut self) -> Result<()> {
        let ids = self.selected_ids_in_order();
        if ids.is_empty() {
            bail!("Select a timeline clip first.");
        }

        let mut replacements = HashMap::new();
        for id in &ids {
            let clip = clip_by_id(&self.model, id)
                .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
            let local = self.position - num_at(clip, "start", 0.0);
            let total = clip_duration(clip);
            if !(local > 0.02 && local < total - 0.02) {
                bail!("Place the playhead inside a selected clip.");
            }
            let first = slice_clip(clip, 0.0, local);
            let mut second = slice_clip(clip, local, total);
            let second_id = uid();
            set_string(&mut second, "id", &second_id);
            replacements.insert(id.clone(), (first, second, second_id));
        }

        let old_primary = self.primary.clone();
        let mut new_selection = HashSet::new();
        let mut new_primary = None;
        self.checkpoint();
        for id in &ids {
            let Some((track, clip)) = clip_location(&self.model, id) else {
                continue;
            };
            let Some((first, second, second_id)) = replacements.remove(id) else {
                continue;
            };
            if let Some(clips) = self
                .model
                .get_mut("tracks")
                .and_then(|value| value.get_mut(track))
                .and_then(|value| value.get_mut("clips"))
                .and_then(Value::as_array_mut)
            {
                clips.splice(clip..clip + 1, [first, second]);
                new_selection.insert(second_id.clone());
                if old_primary.as_deref() == Some(id.as_str()) {
                    new_primary = Some(second_id);
                }
            }
        }
        self.selected = new_selection;
        self.primary = new_primary.or_else(|| self.selected.iter().next().cloned());
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
        Ok(())
    }

    pub fn delete_selected(&mut self) {
        if self.selected.is_empty() {
            return;
        }
        self.checkpoint();
        if let Some(tracks) = self.model.get_mut("tracks").and_then(Value::as_array_mut) {
            for track in tracks {
                if let Some(clips) = track.get_mut("clips").and_then(Value::as_array_mut) {
                    clips.retain(|clip| {
                        clip.get("id")
                            .and_then(Value::as_str)
                            .map(|id| !self.selected.contains(id))
                            .unwrap_or(true)
                    });
                }
            }
        }
        self.selected.clear();
        self.primary = None;
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
    }

    pub fn duplicate_selected(&mut self) {
        if self.selected.is_empty() {
            return;
        }
        self.copy_selected();
        if self.clipboard.is_empty() {
            return;
        }
        self.position = self
            .selected_ids_in_order()
            .iter()
            .filter_map(|id| clip_by_id(&self.model, id))
            .map(|clip| num_at(clip, "start", 0.0) + clip_duration(clip))
            .fold(0.0_f64, f64::max);
        self.paste();
    }

    pub fn copy_selected(&mut self) {
        self.clipboard.clear();
        self.clipboard_tracks.clear();
        if let Some(tracks) = self.model.get("tracks").and_then(Value::as_array) {
            for track in tracks {
                let track_id = text_at(track, "id");
                if let Some(clips) = track.get("clips").and_then(Value::as_array) {
                    for clip in clips {
                        if clip
                            .get("id")
                            .and_then(Value::as_str)
                            .is_some_and(|id| self.selected.contains(id))
                        {
                            self.clipboard.push(clip.clone());
                            self.clipboard_tracks.push(track_id.clone());
                        }
                    }
                }
            }
        }
    }

    pub fn paste(&mut self) {
        if self.clipboard.is_empty() {
            return;
        }
        let first = self
            .clipboard
            .iter()
            .map(|clip| num_at(clip, "start", 0.0))
            .fold(f64::INFINITY, f64::min);
        let position = self.position;
        self.checkpoint();
        self.selected.clear();
        self.primary = None;

        for (index, clip) in self.clipboard.iter().enumerate() {
            let track_index = self.track_for_clip(index);
            let Some(track_index) = track_index else {
                continue;
            };
            let mut pasted = clip.clone();
            let id = uid();
            set_string(&mut pasted, "id", &id);
            set_number(
                &mut pasted,
                "start",
                position + num_at(clip, "start", 0.0) - first,
            );
            if let Some(clips) = self
                .model
                .get_mut("tracks")
                .and_then(|value| value.get_mut(track_index))
                .and_then(|value| value.get_mut("clips"))
                .and_then(Value::as_array_mut)
            {
                clips.push(pasted);
                if self.primary.is_none() {
                    self.primary = Some(id.clone());
                }
                self.selected.insert(id);
            }
        }

        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
    }

    /// Move selected clips on the timeline. Each clip is clamped at zero,
    /// exactly like the drag path in the Python studio.
    pub fn move_selected(&mut self, delta: f64) {
        if self.selected.is_empty() {
            return;
        }
        self.checkpoint();
        if let Some(tracks) = self.model.get_mut("tracks").and_then(Value::as_array_mut) {
            for track in tracks {
                if let Some(clips) = track.get_mut("clips").and_then(Value::as_array_mut) {
                    for clip in clips {
                        if clip
                            .get("id")
                            .and_then(Value::as_str)
                            .is_some_and(|id| self.selected.contains(id))
                        {
                            let start = (num_at(clip, "start", 0.0) + delta).max(0.0);
                            set_number(clip, "start", start);
                        }
                    }
                }
            }
        }
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
    }

    /// Trim the primary selected clip, preserving the animation/fade envelope.
    pub fn trim_selected(&mut self, left: bool, delta: f64) -> Result<()> {
        let id = self
            .selected_id()
            .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
        let (track, clip_index) = clip_location(&self.model, &id)
            .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
        let mut candidate = self
            .model
            .get("tracks")
            .and_then(|value| value.get(track))
            .and_then(|value| value.get("clips"))
            .and_then(|value| value.get(clip_index))
            .cloned()
            .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
        trim_clip(&mut candidate, left, delta);
        validate_clip_bounds(&candidate, &self.project)?;

        let (track, clip_index) = clip_location(&self.model, &id)
            .ok_or_else(|| anyhow!("Select a timeline clip first."))?;
        self.checkpoint();
        if let Some(slot) = self
            .model
            .get_mut("tracks")
            .and_then(|value| value.get_mut(track))
            .and_then(|value| value.get_mut("clips"))
            .and_then(Value::as_array_mut)
            .and_then(|clips| clips.get_mut(clip_index))
        {
            *slot = candidate;
        }
        self.revision = self.revision.saturating_add(1);
        self.sync_studio();
        Ok(())
    }

    fn selected_id(&self) -> Option<String> {
        if let Some(primary) = &self.primary
            && self.selected.contains(primary)
            && clip_location(&self.model, primary).is_some()
        {
            return Some(primary.clone());
        }
        for track in self.model.get("tracks").and_then(Value::as_array)? {
            for clip in track.get("clips").and_then(Value::as_array)? {
                if let Some(id) = clip.get("id").and_then(Value::as_str)
                    && self.selected.contains(id)
                {
                    return Some(id.to_owned());
                }
            }
        }
        None
    }

    fn selected_ids_in_order(&self) -> Vec<String> {
        let mut result = Vec::new();
        if let Some(tracks) = self.model.get("tracks").and_then(Value::as_array) {
            for track in tracks {
                if let Some(clips) = track.get("clips").and_then(Value::as_array) {
                    for clip in clips {
                        if let Some(id) = clip.get("id").and_then(Value::as_str)
                            && self.selected.contains(id)
                        {
                            result.push(id.to_owned());
                        }
                    }
                }
            }
        }
        result
    }

    fn track_for_clip(&self, clipboard_index: usize) -> Option<usize> {
        let tracks = self.model.get("tracks").and_then(Value::as_array)?;
        let wanted_track = self.clipboard_tracks.get(clipboard_index);
        if let Some(wanted) = wanted_track
            && !wanted.is_empty()
            && let Some(index) = tracks
                .iter()
                .position(|track| text_at(track, "id") == *wanted)
        {
            return Some(index);
        }
        let clip = self.clipboard.get(clipboard_index)?;
        let source_id = clip.get("source_id").and_then(Value::as_str)?;
        if let Some(index) = tracks
            .iter()
            .position(|track| track.get("source_id").and_then(Value::as_str) == Some(source_id))
        {
            return Some(index);
        }
        if tracks.len() == 1 {
            return Some(0);
        }
        let kind = self
            .project
            .get("sources")
            .and_then(Value::as_array)?
            .iter()
            .find(|source| source.get("id").and_then(Value::as_str) == Some(source_id))
            .map(|source| text_at(source, "type"))?;
        tracks
            .iter()
            .position(|track| text_at(track, "kind") == kind)
    }

    fn prune_selection(&mut self) {
        self.selected
            .retain(|id| clip_location(&self.model, id).is_some());
        if let Some(primary) = &self.primary
            && !self.selected.contains(primary)
        {
            self.primary = None;
        }
    }

    fn sync_studio(&mut self) {
        if self.project.is_object() {
            set_value(&mut self.project, "studio", self.model.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn sample_project() -> Value {
        json!({
            "format": "katman",
            "version": 1,
            "duration": 2.0,
            "name": "Example",
            "fps": 60,
            "custom_project_field": {"keep": true},
            "sources": [
                {
                    "id": "left", "name": "Left", "type": "video",
                    "file": "media/left.mkv", "width": 160, "height": 90,
                    "logical_width": 160, "logical_height": 90,
                    "x": 0, "y": 0, "media_duration": 2,
                    "timeline_start": 0, "trim_in": 0,
                    "custom_source_field": "unchanged"
                },
                {
                    "id": "right", "name": "Right", "type": "video",
                    "file": "media/right.mkv", "width": 90, "height": 160,
                    "logical_width": 90, "logical_height": 160,
                    "x": 160, "y": 0, "media_duration": 2,
                    "timeline_start": 0, "trim_in": 0
                },
                {
                    "id": "game", "name": "Game", "type": "audio",
                    "file": "media/game.mkv", "media_duration": 2,
                    "timeline_start": 0, "trim_in": 0
                }
            ]
        })
    }

    fn write_project(directory: &Path, project: &Value) -> PathBuf {
        fs::create_dir_all(directory.join("media")).unwrap();
        let path = directory.join("project.katman");
        fs::write(&path, serde_json::to_vec_pretty(project).unwrap()).unwrap();
        path
    }

    fn assert_json_close(actual: &Value, expected: &Value, tolerance: f64) {
        match (actual, expected) {
            (Value::Object(actual), Value::Object(expected)) => {
                assert_eq!(
                    actual.len(),
                    expected.len(),
                    "object size: {actual:?} vs {expected:?}"
                );
                for (key, value) in actual {
                    let other = expected
                        .get(key)
                        .unwrap_or_else(|| panic!("missing key {key} in {expected:?}"));
                    assert_json_close(value, other, tolerance);
                }
            }
            (Value::Array(actual), Value::Array(expected)) => {
                assert_eq!(
                    actual.len(),
                    expected.len(),
                    "array size: {actual:?} vs {expected:?}"
                );
                for (left, right) in actual.iter().zip(expected) {
                    assert_json_close(left, right, tolerance);
                }
            }
            (Value::Number(actual), Value::Number(expected)) => {
                let left = actual.as_f64().unwrap();
                let right = expected.as_f64().unwrap();
                assert!(
                    (left - right).abs() <= tolerance,
                    "numbers differ: {left} vs {right}"
                );
            }
            _ => assert_eq!(actual, expected),
        }
    }

    fn canonical(value: &Value) -> Value {
        match value {
            Value::Object(object) => {
                let mut result = Map::new();
                for (key, value) in object {
                    if key == "id" {
                        result.insert(key.clone(), Value::String("<id>".to_owned()));
                    } else {
                        result.insert(key.clone(), canonical(value));
                    }
                }
                Value::Object(result)
            }
            Value::Array(items) => Value::Array(items.iter().map(canonical).collect()),
            Value::Number(number) => number
                .as_f64()
                .and_then(serde_json::Number::from_f64)
                .map(Value::Number)
                .unwrap_or(Value::Null),
            other => other.clone(),
        }
    }

    // Generated by running katman/studio_model.py on the same project and
    // algorithm cases. IDs are normalised by the comparison helper.
    const PARITY_FIXTURE: &str = r##"{"clip_duration":[{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"expected":5.0},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"expected":4.0},{"clip":{"duration":2,"freeze":true,"freeze_at":1.5,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"expected":2.0}],"model":{"annotations":[{"color":"#ff0000","custom":"keep","end":0.625,"h":730,"id":"<id>","kind":"redact","points":[[0.1,0.2],[0.3,0.4]],"start":0,"tool":"redact","w":1640,"x":0.0,"y":13.0},{"end":0.625,"font_size":42,"h":730,"id":"<id>","kind":"text","start":0,"text":"Hello","tool":"text","w":1640,"x":0.0,"y":13.0}],"fps":59,"height":756,"snap":true,"subtitle_style":{"color":"#ffffff","font":"Noto Sans","size":34},"subtitles":[],"tracks":[{"clips":[{"blackout":false,"duration":0.525,"gain":1,"h":720,"id":"<id>","in":0,"keys":{},"length":1.05,"name":"Screen","opacity":1,"rotation":0,"source_id":"screen","speed":2,"start":0.1,"w":1280,"x":0.0,"y":13.0},{"blackout":false,"duration":3.0,"gain":1,"h":1080,"id":"<id>","in":1.1500000000000001,"keys":{},"length":1.5,"name":"Screen","opacity":1,"rotation":0,"source_id":"screen","speed":0.5,"start":0.625,"w":1920,"x":-44.0,"y":-108.0}],"id":"<id>","kind":"video","mute":false,"name":"Screen","source_id":"screen","visible":true},{"clips":[{"blackout":true,"duration":0.625,"gain":1,"h":480,"id":"<id>","in":0.25,"keys":{},"length":1.25,"name":"Camera","opacity":1,"rotation":0,"source_id":"cam","speed":2,"start":0.0,"w":640,"x":1000.0,"y":263.0}],"id":"<id>","kind":"video","mute":false,"name":"Camera","source_id":"cam","visible":true},{"clips":[{"duration":0.625,"gain":0.5,"id":"<id>","in":0,"keys":{},"length":1.25,"name":"Microphone","source_id":"mic","speed":2,"start":0.0},{"duration":3.0,"gain":1,"id":"<id>","in":1.25,"keys":{},"length":1.5,"name":"Microphone","source_id":"mic","speed":0.5,"start":0.625}],"id":"<id>","kind":"audio","mute":false,"name":"Microphone","source_id":"mic","visible":true}],"version":1,"width":1640},"project":{"custom":{"keep":[1,2]},"duration":3,"format":"katman","fps":59.94,"name":"Parity","sections":[{"annotations":[{"color":"#ff0000","custom":"keep","end":0.8,"points":[[0.1,0.2],[0.3,0.4]],"start":0.1,"tool":"redact"},{"end":1,"font_size":42,"start":0,"text":"Hello","tool":"text"}],"blackout_ids":["cam"],"enabled_ids":["screen","cam","mic"],"end":1.25,"gains":{"mic":0.5},"layer_order":["cam","screen"],"placements":{"cam":[1000,250,640,480],"screen":[0,0,1280,720]},"speed":2,"start":0},{"annotations":[],"crop":[0.05,0.1,0.8,0.7],"enabled_ids":["screen","mic"],"end":2.75,"gains":{},"speed":0.5,"start":1.25}],"sources":[{"backend":"portal","enabled":true,"file":"media/screen.mkv","gain":1,"height":1080,"id":"screen","logical_height":900,"logical_width":1600,"media_duration":3,"name":"Screen","timeline_start":0.2,"trim_in":0.1,"type":"video","width":1920,"x":-40,"y":10},{"enabled":true,"file":"media/cam.mkv","gain":0.7,"height":480,"id":"cam","logical_height":480,"logical_width":640,"media_duration":2.5,"name":"Camera","timeline_start":0,"trim_in":0.25,"type":"video","width":640,"x":1200,"y":300},{"enabled":true,"file":"media/mic.flac","gain":1.25,"id":"mic","media_duration":3,"name":"Microphone","timeline_start":0,"trim_in":0,"type":"audio"}],"version":1},"source_offset":[{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"expected":1.0,"time":0},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"expected":1.5,"time":1},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"expected":3.0,"time":4},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"expected":4.0,"time":1},{"clip":{"duration":2,"freeze":true,"freeze_at":1.5,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"expected":1.5,"time":1}],"split":[{"clip":{"duration":4,"effect_length":4,"effect_offset":0,"fade_in":2,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"expected":[{"duration":2,"effect_length":4,"effect_offset":0,"fade_in":2,"id":"<id>","in":1.0,"keys":{"x":[[0,0.0],[2,50.0]]},"length":2.0,"name":"clip","source_id":"media","speed":1,"start":0},{"duration":2.0,"effect_length":4,"effect_offset":2,"fade_in":2,"id":"<id>","in":3.0,"keys":{"x":[[0,50.0],[2.0,100.0]]},"length":2.0,"name":"clip","source_id":"media","speed":1,"start":2}],"name":"fade","position":2},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"expected":[{"duration":2,"effect_length":5.0,"effect_offset":0,"id":"<id>","in":1.0,"keys":{"x":[[0,0.0],[2,50.0]]},"length":1.0,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5]],"start":0},{"duration":3.0,"effect_length":5.0,"effect_offset":2,"id":"<id>","in":2.0,"keys":{"x":[[0,50.0],[2,100],[3.0,100.0]]},"length":3.0,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[1.0,2]],"start":2}],"name":"speed","position":2},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"expected":[{"duration":1,"effect_length":4.0,"effect_offset":0,"id":"<id>","in":4.0,"keys":{"x":[[0,0.0],[1,25.0]]},"length":1.0,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},{"duration":3.0,"effect_length":4.0,"effect_offset":1,"id":"<id>","in":1.0,"keys":{"x":[[0,25.0],[3.0,100.0]]},"length":3.0,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":1}],"name":"reverse","position":1}],"trim":[{"clip":{"duration":4,"effect_length":4,"effect_offset":0,"fade_in":2,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"delta":0.5,"expected":{"duration":3.5,"effect_length":4,"effect_offset":0.5,"fade_in":2,"id":"<id>","in":1.5,"keys":{"x":[[0,12.5],[3.5,100.0]]},"length":3.5,"name":"clip","source_id":"media","speed":1,"start":0.5},"name":"left_slice","side":"left"},{"clip":{"duration":4,"effect_length":4,"effect_offset":0,"fade_in":2,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"delta":-0.5,"expected":{"duration":3.5,"effect_length":4,"effect_offset":0,"fade_in":2,"id":"<id>","in":1.0,"keys":{"x":[[0,0.0],[3.5,87.5]]},"length":3.5,"name":"clip","source_id":"media","speed":1,"start":0},"name":"right_slice","side":"right"},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"delta":-0.5,"expected":{"duration":5.5,"id":"<id>","in":0.75,"keys":{"x":[[0,0],[4,100]]},"length":4.25,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[0.25,0.5],[2.25,2]],"start":0},"name":"left_extend","side":"left"},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"delta":0.5,"expected":{"duration":5.5,"id":"<id>","in":1,"keys":{"x":[[0,0],[4,100]]},"length":5.0,"name":"clip","source_id":"media","speed":1,"speed_keys":[[0,0.5],[2,2]],"start":0},"name":"right_extend","side":"right"},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"delta":0.5,"expected":{"duration":3.5,"effect_length":4.0,"effect_offset":0.5,"id":"<id>","in":1.0,"keys":{"x":[[0,12.5],[3.5,100.0]]},"length":3.5,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0.5},"name":"reverse_left","side":"left"},{"clip":{"duration":4,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"delta":0.5,"expected":{"duration":4.5,"id":"<id>","in":0.5,"keys":{"x":[[0,0],[4,100]]},"length":4.5,"name":"clip","reverse":true,"source_id":"media","speed":1,"start":0},"name":"reverse_right","side":"right"},{"clip":{"duration":2,"freeze":true,"freeze_at":1.5,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"delta":-0.5,"expected":{"duration":2.0,"freeze":true,"freeze_at":1.5,"id":"<id>","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"name":"freeze_left","side":"left"},{"clip":{"duration":2,"freeze":true,"freeze_at":1.5,"id":"a","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"delta":0.7,"expected":{"duration":2.7,"freeze":true,"freeze_at":1.5,"id":"<id>","in":1,"keys":{"x":[[0,0],[4,100]]},"length":4,"name":"clip","source_id":"media","speed":1,"start":0},"name":"freeze_right","side":"right"}],"value_at":[{"default":0,"expected":5,"item":{"keys":{"x":[[0,0],[2,10]]}},"key":"x","time":1},{"default":0,"expected":0,"item":{"keys":{"x":[[2,5],[0,0]]}},"key":"x","time":-1},{"default":3,"expected":7,"item":{"keys":{},"y":7},"key":"y","time":1}]}"##;

    #[test]
    fn uid_matches_python_shape() {
        let first = uid();
        let second = uid();
        assert_eq!(first.len(), 12);
        assert!(first.chars().all(|character| character.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }

    #[test]
    fn python_parity_projection_timing_and_slicing() {
        let fixture: Value = serde_json::from_str(PARITY_FIXTURE).unwrap();
        let actual_model = from_project(&fixture["project"]).unwrap();
        assert_json_close(
            &canonical(&actual_model),
            &canonical(&fixture["model"]),
            1e-9,
        );

        for case in fixture["value_at"].as_array().unwrap() {
            let actual = value_at(
                &case["item"],
                case["key"].as_str().unwrap(),
                case["time"].as_f64().unwrap(),
                case["default"].as_f64().unwrap(),
            );
            let expected = case["expected"].as_f64().unwrap();
            assert!((actual - expected).abs() < 1e-12, "value_at case: {case}");
        }

        for case in fixture["clip_duration"].as_array().unwrap() {
            let actual = clip_duration(&case["clip"]);
            let expected = case["expected"].as_f64().unwrap();
            assert!((actual - expected).abs() < 1e-12, "duration case: {case}");
        }

        for case in fixture["source_offset"].as_array().unwrap() {
            let actual = source_offset(&case["clip"], case["time"].as_f64().unwrap());
            let expected = case["expected"].as_f64().unwrap();
            assert!((actual - expected).abs() < 1e-12, "offset case: {case}");
        }

        for case in fixture["trim"].as_array().unwrap() {
            let mut clip = case["clip"].clone();
            trim_clip(
                &mut clip,
                case["side"].as_str() == Some("left"),
                case["delta"].as_f64().unwrap(),
            );
            assert_json_close(&canonical(&clip), &canonical(&case["expected"]), 1e-9);
        }

        for case in fixture["split"].as_array().unwrap() {
            let mut model = json!({
                "tracks": [{"id": "track", "clips": [case["clip"].clone()]}]
            });
            split_clip(&mut model, "a", case["position"].as_f64().unwrap()).unwrap();
            assert_json_close(
                &canonical(&model["tracks"][0]["clips"]),
                &canonical(&case["expected"]),
                1e-9,
            );
        }
    }

    #[test]
    fn value_at_interpolates_and_clamps() {
        let item = json!({"keys": {"x": [[0.0, 0.0], [2.0, 10.0]]}, "y": 7.0});
        assert_eq!(value_at(&item, "x", -1.0, 0.0), 0.0);
        assert_eq!(value_at(&item, "x", 1.0, 0.0), 5.0);
        assert_eq!(value_at(&item, "x", 3.0, 0.0), 10.0);
        assert_eq!(value_at(&item, "y", 1.0, 3.0), 7.0);
        assert_eq!(value_at(&item, "missing", 1.0, 3.0), 3.0);
    }

    #[test]
    fn variable_speed_duration_and_source_offset() {
        let clip = json!({
            "in": 1.0, "length": 4.0, "duration": 4.0, "speed": 1.0,
            "speed_keys": [[0.0, 0.5], [2.0, 2.0]]
        });
        assert!((clip_duration(&clip) - 5.0).abs() < 1e-9);
        assert!((source_offset(&clip, 1.0) - 1.5).abs() < 1e-9);
        assert!((source_offset(&clip, 4.0) - 3.0).abs() < 1e-9);
    }

    #[test]
    fn reverse_and_freeze_source_offsets() {
        let reverse = json!({
            "in": 0.0, "length": 4.0, "duration": 4.0, "speed": 1.0,
            "reverse": true
        });
        assert!((source_offset(&reverse, 0.0) - 4.0).abs() < 1e-9);
        assert!((source_offset(&reverse, 1.0) - 3.0).abs() < 1e-9);
        let freeze = json!({
            "in": 0.5, "freeze": true, "freeze_at": 1.5, "duration": 2.0
        });
        assert_eq!(source_offset(&freeze, 1.0), 1.5);
        assert_eq!(clip_duration(&freeze), 2.0);
    }

    #[test]
    fn split_preserves_animation_and_fade_clock() {
        let clip = json!({
            "id": "a", "source_id": "media", "name": "clip",
            "start": 0.0, "in": 0.0, "length": 4.0, "duration": 4.0,
            "speed": 1.0, "fade_in": 2.0,
            "keys": {"x": [[0.0, 0.0], [4.0, 100.0]]}
        });
        let mut model = json!({"tracks": [{"clips": [clip]}]});
        let second_id = split_clip(&mut model, "a", 2.0).unwrap();
        let clips = model["tracks"][0]["clips"].as_array().unwrap();
        assert_eq!(clips[0]["keys"]["x"][1][1], 50.0);
        assert_eq!(clips[1]["keys"]["x"][1][1], 100.0);
        assert_eq!(clips[1]["effect_offset"], 2.0);
        assert_eq!(clips[1]["id"], Value::String(second_id));
        let mut second = clips[1].clone();
        trim_clip(&mut second, true, 0.5);
        assert_eq!(value_at(&second, "x", 0.0, 0.0), 62.5);
        assert_eq!(value_at(&second, "x", 1.5, 0.0), 100.0);
        assert_eq!(second["effect_offset"], 2.5);
    }

    #[test]
    fn segmented_speed_split_preserves_source_and_total_duration() {
        let clip = json!({
            "id": "a", "source_id": "media", "name": "clip",
            "start": 0.0, "in": 1.0, "length": 4.0, "duration": 4.0,
            "speed": 1.0, "speed_keys": [[0.0, 0.5], [2.0, 2.0]],
            "keys": {"x": [[0.0, 0.0], [5.0, 100.0]]}
        });
        let mut model = json!({"tracks": [{"clips": [clip]}]});
        let total = clip_duration(&model["tracks"][0]["clips"][0]);
        split_clip(&mut model, "a", 2.0).unwrap();
        let clips = model["tracks"][0]["clips"].as_array().unwrap();
        assert!((clip_duration(&clips[0]) + clip_duration(&clips[1]) - total).abs() < 1e-9);
        assert!((clips[1]["in"].as_f64().unwrap() - 2.0).abs() < 1e-9);
        assert_eq!(clips[1]["keys"]["x"][0][1], 40.0);
    }

    #[test]
    fn reverse_split_preserves_playback_order() {
        let clip = json!({
            "id": "a", "source_id": "media", "name": "clip",
            "start": 0.0, "in": 0.0, "length": 4.0, "duration": 4.0,
            "speed": 1.0, "reverse": true
        });
        let mut model = json!({"tracks": [{"clips": [clip]}]});
        split_clip(&mut model, "a", 1.0).unwrap();
        let clips = model["tracks"][0]["clips"].as_array().unwrap();
        assert_eq!(
            (clips[0]["in"].as_f64(), clips[0]["length"].as_f64()),
            (Some(3.0), Some(1.0))
        );
        assert_eq!(
            (clips[1]["in"].as_f64(), clips[1]["length"].as_f64()),
            (Some(0.0), Some(3.0))
        );
    }

    #[test]
    fn freeze_trim_extends_hold_without_touching_media() {
        let mut clip = json!({
            "id": "a", "source_id": "media", "start": 1.0, "in": 0.5,
            "length": 5.0, "duration": 2.0, "speed": 1.0, "freeze": true,
            "freeze_at": 2.0
        });
        let original_in = clip["in"].clone();
        trim_clip(&mut clip, true, -0.5);
        assert_eq!(clip["start"], 0.5);
        assert_eq!(clip["duration"], 2.5);
        assert_eq!(clip["in"], original_in);
        trim_clip(&mut clip, false, 1.0);
        assert_eq!(clip["duration"], 3.5);
    }

    #[test]
    fn from_project_uses_sections_geometry_and_annotations() {
        let mut project = sample_project();
        project["sections"] = json!([{
            "start": 0.0,
            "end": 2.0,
            "speed": 2.0,
            "enabled_ids": ["left", "right", "game"],
            "gains": {"game": 0.5},
            "placements": {
                "left": [10, 20, 80, 45],
                "right": [100, 20, 90, 160]
            },
            "blackout_ids": ["right"],
            "annotations": [{
                "tool": "redact", "start": 0.2, "end": 1.2,
                "points": [[0.1, 0.1]], "custom_stroke": 9
            }]
        }]);
        let model = from_project(&project).unwrap();

        // scene_layout normalizes the placement origin, leaving a 180 x 160 scene.
        assert_eq!(model["width"], 180.0);
        assert_eq!(model["height"], 160.0);
        assert_eq!(model["fps"], 60);
        assert_eq!(model["tracks"].as_array().unwrap().len(), 3);
        let video = &model["tracks"][0]["clips"][0];
        assert_eq!(video["w"], 80.0);
        assert_eq!(video["h"], 45.0);
        assert_eq!(video["duration"], 1.0);
        assert_eq!(video["gain"], 1.0);
        assert_eq!(video["blackout"], false);
        let right = &model["tracks"][1]["clips"][0];
        assert_eq!(right["blackout"], true);
        let audio = &model["tracks"][2]["clips"][0];
        assert_eq!(audio["gain"], 0.5);
        let annotation = &model["annotations"][0];
        assert_eq!(annotation["kind"], "redact");
        assert_eq!(annotation["end"], 1.0);
        assert_eq!(annotation["custom_stroke"], 9);
        assert_eq!(annotation["w"], 180.0);
    }

    #[test]
    fn from_project_prefers_existing_studio_and_keeps_unknown_fields() {
        let mut project = sample_project();
        project["studio"] = json!({"version": 1, "tracks": [], "future": {"x": 1}});
        let model = from_project(&project).unwrap();
        assert_eq!(model["future"]["x"], 1);
        assert_eq!(model["tracks"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn load_accepts_directory_and_save_preserves_project() {
        let directory = tempdir().unwrap();
        let project = sample_project();
        let path = write_project(directory.path(), &project);
        let mut document = Document::load(directory.path()).unwrap();
        assert_eq!(document.path, fs::canonicalize(&path).unwrap());
        assert_eq!(document.model["width"], 250.0);
        document.model["future_model_field"] = json!({"keep": true});
        document.save().unwrap();

        let saved: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["custom_project_field"]["keep"], true);
        assert_eq!(saved["sources"], project["sources"]);
        assert_eq!(saved["studio"]["future_model_field"]["keep"], true);
    }

    #[test]
    fn crossfade_is_one_undo_step_and_preserves_source_metadata(){
        let directory=tempdir().unwrap();let project=sample_project();let path=write_project(directory.path(),&project);let mut doc=Document::load(&path).unwrap();
        let first=doc.model["tracks"][0]["clips"][0]["id"].as_str().unwrap().to_string();doc.selected.insert(first);doc.duplicate_selected();
        doc.selected=doc.model["tracks"][0]["clips"].as_array().unwrap().iter().filter_map(|c|c["id"].as_str().map(str::to_string)).collect();
        let before=doc.model.clone();doc.crossfade_selected(0.25).unwrap();let clips=doc.model["tracks"][0]["clips"].as_array().unwrap();assert_eq!(clips[1]["fade_in"],0.25);assert_eq!(num_at(&clips[1],"start",0.),num_at(&clips[0],"start",0.)+clip_duration(&clips[0])-0.25);
        assert_eq!(doc.project["sources"],project["sources"]);doc.undo();assert_eq!(doc.model,before);
    }

    #[test]
    fn load_rejects_zero_canvas_before_it_can_reach_layout() {
        let directory = tempdir().unwrap();
        let mut project = sample_project();
        let mut model = from_project(&project).unwrap();
        model["width"] = json!(0);
        project["studio"] = model;
        let path = write_project(directory.path(), &project);
        assert!(Document::load(&path).is_err());
    }

    #[test]
    fn load_rejects_unsupported_format_and_media_escape() {
        let directory = tempdir().unwrap();
        let mut project = sample_project();
        project["version"] = json!(2);
        let path = write_project(directory.path(), &project);
        assert!(Document::load(&path).is_err());

        let mut project = sample_project();
        project["sources"][0]["file"] = json!("../outside.mkv");
        let path = write_project(directory.path(), &project);
        let error = match Document::load(&path) {
            Ok(_) => panic!("escaped media path was accepted"),
            Err(error) => error.to_string(),
        };
        assert!(error.contains("inside the project folder"));
    }

    #[test]
    fn checkpoint_undo_redo_and_selection_pruning() {
        let directory = tempdir().unwrap();
        let path = write_project(directory.path(), &sample_project());
        let mut document = Document::load(&path).unwrap();
        let id = document.model["tracks"][0]["clips"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        document.selected.insert(id.clone());
        document.primary = Some(id.clone());
        document.checkpoint();
        document.model["tracks"][0]["clips"][0]["start"] = json!(3.0);
        document.revision += 1;
        assert_eq!(document.selected_clip().unwrap()["id"], id);
        document.undo();
        assert_eq!(document.model["tracks"][0]["clips"][0]["start"], 0.0);
        assert_eq!(document.selected.len(), 1);
        document.redo();
        assert_eq!(document.model["tracks"][0]["clips"][0]["start"], 3.0);
    }

    #[test]
    fn split_delete_move_and_trim_selected() {
        let directory = tempdir().unwrap();
        let path = write_project(directory.path(), &sample_project());
        let mut document = Document::load(&path).unwrap();
        let id = document.model["tracks"][0]["clips"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        document.selected.insert(id.clone());
        document.primary = Some(id.clone());
        document.position = 1.0;
        document.split_selected().unwrap();
        assert_eq!(
            document.model["tracks"][0]["clips"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(document.selected.len(), 1);
        let second = document.selected_clip().unwrap().clone();
        assert_eq!(second["start"], 1.0);

        document.move_selected(-5.0);
        assert_eq!(document.selected_clip().unwrap()["start"], 0.0);

        document.trim_selected(true, 0.25).unwrap();
        assert_eq!(document.selected_clip().unwrap()["start"], 0.25);

        document.delete_selected();
        assert_eq!(
            document.model["tracks"][0]["clips"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(document.selected.is_empty());
        assert!(document.primary.is_none());
    }

    #[test]
    fn trim_rejects_source_overrun_without_mutating_or_checkpointing() {
        let directory = tempdir().unwrap();
        let path = write_project(directory.path(), &sample_project());
        let mut document = Document::load(&path).unwrap();
        let id = document.model["tracks"][0]["clips"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        document.selected.insert(id.clone());
        document.primary = Some(id);
        let before = document.model.clone();
        // Extending right by 10s exceeds media_duration=2.
        assert!(document.trim_selected(false, 10.0).is_err());
        assert_eq!(canonical(&document.model), canonical(&before));
        assert!(document.undo_stack.is_empty());
    }

    #[test]
    fn copy_paste_and_duplicate_use_new_ids_and_track_mapping() {
        let directory = tempdir().unwrap();
        let path = write_project(directory.path(), &sample_project());
        let mut document = Document::load(&path).unwrap();
        let id = document.model["tracks"][0]["clips"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        document.selected.insert(id.clone());
        document.primary = Some(id.clone());
        document.copy_selected();
        assert_eq!(document.clipboard.len(), 1);
        document.position = 3.0;
        document.paste();
        let clips = document.model["tracks"][0]["clips"].as_array().unwrap();
        assert_eq!(clips.len(), 2);
        assert_ne!(clips[0]["id"], clips[1]["id"]);
        assert_eq!(clips[1]["start"], 3.0);
        assert_eq!(clips[1]["source_id"], "left");

        document.selected = HashSet::from([id]);
        document.primary = document.selected.iter().next().cloned();
        document.duplicate_selected();
        let clips = document.model["tracks"][0]["clips"].as_array().unwrap();
        assert_eq!(clips.len(), 3);
        // The original selected clip is duplicated immediately after itself.
        assert_eq!(clips[2]["start"], 2.0);
    }

    #[test]
    fn variable_speed_and_reverse_trim_semantics_match_python() {
        let mut clip = json!({
            "id": "a", "source_id": "media", "start": 0.0, "in": 1.0,
            "length": 4.0, "duration": 4.0, "speed": 1.0,
            "speed_keys": [[0.0, 0.5], [2.0, 2.0]],
            "keys": {"x": [[0.0, 0.0], [5.0, 100.0]]}
        });
        trim_clip(&mut clip, true, -0.5);
        assert!(clip["in"].as_f64().unwrap() < 1.0);
        assert_eq!(clip["speed_keys"][0][1], 0.5);
        let mut reverse = json!({
            "id": "b", "source_id": "media", "start": 0.0, "in": 1.0,
            "length": 4.0, "duration": 4.0, "speed": 1.0, "reverse": true
        });
        trim_clip(&mut reverse, false, 0.5);
        assert!((reverse["in"].as_f64().unwrap() - 0.5).abs() < 1e-9);
    }

    #[test]
    fn python_round_and_even_crop_helpers() {
        assert_eq!(python_round(2.5), 2.0);
        assert_eq!(python_round(3.5), 4.0);
        let section = json!({"crop": [0.1, 0.1, 0.5, 0.5]});
        assert_eq!(
            crop_geometry(101.0, 101.0, &section),
            (10.0, 10.0, 50.0, 50.0)
        );
    }
}
