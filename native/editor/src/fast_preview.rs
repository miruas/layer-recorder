// SPDX-License-Identifier: GPL-3.0-only
//! Interactive source-frame cache. Geometry and vector edits are GPU draws, not
//! FFmpeg renders. Unsupported effects deliberately use the accurate compositor.
use crate::model;
use eframe::egui::{self, Color32, Pos2, Rect, Stroke, Vec2};
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet, VecDeque},
    hash::Hash,
    io::Read,
    os::fd::AsRawFd,
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Condvar, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};
const MAX_CACHE_BYTES: usize = 64 * 1024 * 1024;
const MAX_LAYERS: usize = 32;
#[derive(Clone, Debug, Hash, Eq, PartialEq)]
pub struct Key {
    pub path: PathBuf,
    pub root: PathBuf,
    pub time_us: u64,
    pub width: usize,
    pub height: usize,
}
#[derive(Clone)]
pub struct Layer {
    pub key: Option<Key>,
    pub rect: [f32; 4],
    pub uv: [f32; 4],
    pub angle: f32,
    pub alpha: f32,
    pub border: f32,
    pub border_color: Color32,
}
#[derive(Clone)]
pub enum Mark {
    Fill([f32; 4], Color32),
    Line(Vec<[f32; 2]>, Color32, f32, bool),
}
#[derive(Clone)]
pub struct Scene {
    pub layers: Vec<Layer>,
    pub marks: Vec<Mark>,
}
fn n(v: &Value, k: &str, d: f64) -> f64 {
    v[k].as_f64().filter(|x| x.is_finite()).unwrap_or(d)
}
fn at(v: &Value, k: &str, t: f64, d: f64) -> f32 {
    model::value_at(v, k, t, d) as f32
}
fn color(raw: &str, alpha: f32) -> Color32 {
    let s = raw.trim_start_matches('#');
    let x = u32::from_str_radix(s, 16).unwrap_or(0xffffff);
    Color32::from_rgba_unmultiplied(
        (x >> 16) as u8,
        (x >> 8) as u8,
        x as u8,
        (alpha.clamp(0., 1.) * 255.).round() as u8,
    )
}
fn active(v: &Value, t: f64) -> bool {
    t >= n(v, "start", 0.) && t < n(v, "end", f64::INFINITY)
}
fn safe_file(root: &Path, relative: &str) -> Option<PathBuf> {
    let p = Path::new(relative);
    if p.components().any(|c| {
        matches!(
            c,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    }) {
        None
    } else {
        Some(root.join(p))
    }
}
/// No unsupported visual effect is silently skipped, including privacy effects.
pub fn scene(path: &Path, project: &Value, model: &Value, time: f64) -> Option<Scene> {
    if !time.is_finite() || time < 0. {
        return None;
    }
    let root = path.parent()?;
    let mut budget = 0usize;
    let mut working_set = HashSet::new();
    let mut result = Scene {
        layers: vec![],
        marks: vec![],
    };
    if model["subtitles"]
        .as_array()
        .is_some_and(|xs| xs.iter().any(|s| active(s, time)))
    {
        return None;
    }
    for track in model["tracks"].as_array()? {
        if track["kind"] != "video" || track["visible"] == false {
            continue;
        }
        for clip in track["clips"].as_array()? {
            let local = time - n(clip, "start", 0.);
            if local < 0. || local >= model::clip_duration(clip) || clip["mute"] == true {
                continue;
            }
            if ["stabilization", "lut"].iter().any(|k| {
                !matches!(&clip[k], Value::Null | Value::Bool(false))
                    && clip[k].as_str() != Some("")
            }) {
                return None;
            }
            for (key, default) in [
                ("brightness", 0.),
                ("contrast", 1.),
                ("saturation", 1.),
                ("gamma", 1.),
                ("temperature", 0.),
                ("tint", 0.),
                ("sharpness", 0.),
            ] {
                if n(clip, key, default) != default {
                    return None;
                }
            }
            // Reverse frame selection needs the actual source frame interval.
            if clip["reverse"] == true && clip["freeze"] != true {
                return None;
            }
            let source = project["sources"]
                .as_array()?
                .iter()
                .find(|s| s["id"] == clip["source_id"])?;
            let sw = n(source, "width", 0.);
            let sh = n(source, "height", 0.);
            if sw < 1. || sh < 1. {
                return None;
            }
            let factor = (960. / sw).min(640. / sh).min(1.);
            let width = (sw * factor).floor().max(1.) as usize;
            let height = (sh * factor).floor().max(1.) as usize;
            let file = safe_file(root, source["file"].as_str()?)?;
            let seek = model::source_offset(clip, local);
            if !seek.is_finite() || seek < 0. || seek >= n(source, "media_duration", f64::MAX) {
                return None;
            }
            let mut alpha = at(clip, "opacity", local, 1.).clamp(0., 1.);
            let effect = local + n(clip, "effect_offset", 0.);
            let fade_in = n(clip, "fade_in", 0.);
            let fade_out = n(clip, "fade_out", 0.);
            if fade_in > 0. {
                alpha *= (effect / fade_in).clamp(0., 1.) as f32;
            }
            if fade_out > 0. {
                alpha *= ((n(clip, "effect_length", model::clip_duration(clip)) - effect)
                    / fade_out)
                    .clamp(0., 1.) as f32;
            }
            if alpha <= 0. {
                continue;
            }
            let mut uv = [
                n(clip, "crop_x", 0.) as f32,
                n(clip, "crop_y", 0.) as f32,
                0.,
                0.,
            ];
            uv[2] = uv[0] + n(clip, "crop_w", 1.) as f32;
            uv[3] = uv[1] + n(clip, "crop_h", 1.) as f32;
            if uv.iter().any(|v| !v.is_finite() || *v < 0. || *v > 1.)
                || uv[2] <= uv[0]
                || uv[3] <= uv[1]
            {
                return None;
            }
            if clip["flip_x"] == true {
                uv.swap(0, 2);
            }
            if clip["flip_y"] == true {
                uv.swap(1, 3);
            }
            let key = if clip["blackout"] == true {
                None
            } else {
                Some(Key {
                    path: file,
                    root: root.to_path_buf(),
                    time_us: (seek * 1e6).round() as u64,
                    width,
                    height,
                })
            };
            if let Some(key) = &key {
                if working_set.insert(key.clone()) {
                    budget = budget.checked_add(width.checked_mul(height)?.checked_mul(4)?)?;
                    if budget > MAX_CACHE_BYTES {
                        return None;
                    }
                }
            }
            result.layers.push(Layer {
                key,
                rect: [
                    at(clip, "x", local, 0.),
                    at(clip, "y", local, 0.),
                    at(clip, "w", local, sw),
                    at(clip, "h", local, sh),
                ],
                uv,
                angle: at(clip, "rotation", local, 0.).to_radians(),
                alpha,
                border: n(clip, "border_width", 0.) as f32,
                border_color: color(clip["border_color"].as_str().unwrap_or("#ffffff"), alpha),
            });
            if result.layers.last().is_some_and(|l| {
                l.rect.iter().any(|v| !v.is_finite())
                    || l.rect[2] <= 0.
                    || l.rect[3] <= 0.
                    || !l.angle.is_finite()
                    || !l.alpha.is_finite()
            }) {
                return None;
            }
            if result.layers.len() > MAX_LAYERS {
                return None;
            }
        }
    }
    for ann in model["annotations"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| active(v, time))
    {
        let local = time - n(ann, "start", 0.);
        let privacy = matches!(ann["kind"].as_str(), Some("redact" | "blur" | "pixelate"));
        let rect = [
            at(ann, "x", local, 0.),
            at(ann, "y", local, 0.),
            at(
                ann,
                "w",
                local,
                if privacy {
                    100.
                } else {
                    n(model, "width", 1920.)
                },
            ),
            at(
                ann,
                "h",
                local,
                if privacy {
                    100.
                } else {
                    n(model, "height", 1080.)
                },
            ),
        ];
        if rect.iter().any(|v| !v.is_finite()) || rect[2] <= 0. || rect[3] <= 0. {
            return None;
        }
        let c = color(
            ann["color"].as_str().unwrap_or("#ffffff"),
            at(ann, "opacity", local, 1.),
        );
        match ann["kind"].as_str()? {
            "redact" => result.marks.push(Mark::Fill(rect, Color32::BLACK)),
            "pen" | "line" | "arrow" | "rectangle" => {
                let pts: Vec<[f32; 2]> = ann["points"]
                    .as_array()?
                    .iter()
                    .map(|p| {
                        [
                            rect[0] + p[0].as_f64().unwrap_or(0.) as f32 * rect[2],
                            rect[1] + p[1].as_f64().unwrap_or(0.) as f32 * rect[3],
                        ]
                    })
                    .collect();
                if pts.len() > 5000 || pts.iter().flatten().any(|v| !v.is_finite()) {
                    return None;
                }
                let width = (n(ann, "width", 0.004) as f32 * rect[3]).max(1.);
                if !width.is_finite() {
                    return None;
                }
                match ann["kind"].as_str().unwrap() {
                    "rectangle" if pts.len() >= 2 => {
                        let a = pts[0];
                        let b = *pts.last().unwrap();
                        result.marks.push(Mark::Line(
                            vec![a, [b[0], a[1]], b, [a[0], b[1]]],
                            c,
                            width,
                            true,
                        ));
                    }
                    "arrow" if pts.len() >= 2 => {
                        let a = pts[0];
                        let b = *pts.last().unwrap();
                        let angle = (b[1] - a[1]).atan2(b[0] - a[0]);
                        let head = (width * 4.).max(10.);
                        result.marks.push(Mark::Line(pts, c, width, false));
                        for delta in [-0.5_f32, 0.5] {
                            result.marks.push(Mark::Line(
                                vec![
                                    b,
                                    [
                                        b[0] - head * (angle + delta).cos(),
                                        b[1] - head * (angle + delta).sin(),
                                    ],
                                ],
                                c,
                                width,
                                false,
                            ));
                        }
                    }
                    _ => result.marks.push(Mark::Line(pts, c, width, false)),
                }
            }
            // The egui font is not FFmpeg's resolved font; drawtext also has an outline.
            "text" => return None,
            _ => return None,
        }
    }
    Some(result)
}
#[derive(Default)]
struct Shared {
    wanted: HashSet<Key>,
    pending: HashSet<Key>,
    queue: VecDeque<Key>,
    stop: bool,
}
struct Pixels {
    key: Key,
    rgba: Result<Vec<u8>, String>,
    elapsed: Duration,
}
struct Texture {
    handle: egui::TextureHandle,
    used: u64,
    bytes: usize,
}
pub struct Cache {
    shared: Arc<(Mutex<Shared>, Condvar)>,
    rx: mpsc::Receiver<Pixels>,
    textures: HashMap<Key, Texture>,
    failed: HashSet<Key>,
    clock: u64,
    pub decoded: u64,
    pub last_decode_ms: f64,
}
impl Cache {
    pub fn new() -> Self {
        let shared = Arc::new((Mutex::new(Shared::default()), Condvar::new()));
        let (tx, rx) = mpsc::sync_channel(4);
        for _ in 0..2 {
            let state = shared.clone();
            let out = tx.clone();
            thread::spawn(move || {
                loop {
                    let key = {
                        let (lock, cv) = &*state;
                        let mut s = lock.lock().unwrap();
                        while s.queue.is_empty() && !s.stop {
                            s = cv.wait(s).unwrap();
                        }
                        if s.stop {
                            return;
                        }
                        s.queue.pop_front().unwrap()
                    };
                    let started = Instant::now();
                    let rgba = decode(&key, &state);
                    if out
                        .send(Pixels {
                            key,
                            rgba,
                            elapsed: started.elapsed(),
                        })
                        .is_err()
                    {
                        return;
                    }
                }
            });
        }
        Self {
            shared,
            rx,
            textures: HashMap::new(),
            failed: HashSet::new(),
            clock: 0,
            decoded: 0,
            last_decode_ms: 0.,
        }
    }
    pub fn request(&mut self, scene: &Scene) {
        self.clock += 1;
        let keys: HashSet<Key> = scene.layers.iter().filter_map(|v| v.key.clone()).collect();
        for key in &keys {
            if let Some(t) = self.textures.get_mut(key) {
                t.used = self.clock;
            }
        }
        let mut s = self.shared.0.lock().unwrap();
        s.wanted = keys.clone();
        let discarded: Vec<Key> = s
            .queue
            .iter()
            .filter(|key| !keys.contains(*key))
            .cloned()
            .collect();
        s.queue.retain(|key| keys.contains(key));
        for key in discarded {
            s.pending.remove(&key);
        }
        for key in keys {
            if !self.textures.contains_key(&key)
                && !self.failed.contains(&key)
                && s.pending.insert(key.clone())
            {
                s.queue.push_back(key);
            }
        }
        self.shared.1.notify_all();
    }
    pub fn poll(&mut self, ctx: &egui::Context) {
        for pixels in self.rx.try_iter() {
            let wanted = {
                let mut state = self.shared.0.lock().unwrap();
                state.pending.remove(&pixels.key);
                state.wanted.contains(&pixels.key)
            };
            if !wanted {
                continue;
            }
            match pixels.rgba {
                Ok(rgba) => {
                    let expected = pixels
                        .key
                        .width
                        .checked_mul(pixels.key.height)
                        .and_then(|v| v.checked_mul(4));
                    if expected != Some(rgba.len()) || rgba.len() > MAX_CACHE_BYTES {
                        self.failed.insert(pixels.key);
                        continue;
                    }
                    let wanted = self.shared.0.lock().unwrap().wanted.clone();
                    while self.textures.values().map(|t| t.bytes).sum::<usize>() + rgba.len()
                        > MAX_CACHE_BYTES
                    {
                        let candidate = self
                            .textures
                            .iter()
                            .filter(|(key, _)| !wanted.contains(*key))
                            .min_by_key(|(_, t)| t.used)
                            .map(|(k, _)| k.clone());
                        if let Some(key) = candidate {
                            self.textures.remove(&key);
                        } else {
                            break;
                        }
                    }
                    if self.textures.values().map(|t| t.bytes).sum::<usize>() + rgba.len()
                        > MAX_CACHE_BYTES
                    {
                        self.failed.insert(pixels.key);
                        continue;
                    }
                    self.decoded += 1;
                    self.last_decode_ms = pixels.elapsed.as_secs_f64() * 1000.;
                    let bytes = rgba.len();
                    let image = egui::ColorImage::from_rgba_unmultiplied(
                        [pixels.key.width, pixels.key.height],
                        &rgba,
                    );
                    let texture =
                        ctx.load_texture("Source frame", image, egui::TextureOptions::LINEAR);
                    self.textures.insert(
                        pixels.key,
                        Texture {
                            handle: texture,
                            used: self.clock,
                            bytes,
                        },
                    );
                }
                Err(reason) => {
                    if reason == "Superseded" {
                        let mut state = self.shared.0.lock().unwrap();
                        if !self.textures.contains_key(&pixels.key)
                            && state.pending.insert(pixels.key.clone())
                        {
                            state.queue.push_back(pixels.key);
                            self.shared.1.notify_all();
                        }
                    } else {
                        if self.failed.len() >= 128 {
                            self.failed.clear();
                        }
                        self.failed.insert(pixels.key);
                    }
                }
            }
        }
        while self.textures.values().map(|t| t.bytes).sum::<usize>() > MAX_CACHE_BYTES {
            let candidate = self
                .textures
                .iter()
                .filter(|(key, _)| !self.shared.0.lock().unwrap().wanted.contains(*key))
                .min_by_key(|(_, t)| t.used)
                .map(|(k, _)| k.clone());
            if let Some(key) = candidate {
                self.textures.remove(&key);
            } else {
                break;
            }
        }
    }
    pub fn failed(&self, scene: &Scene) -> bool {
        scene
            .layers
            .iter()
            .filter_map(|l| l.key.as_ref())
            .any(|key| self.failed.contains(key))
    }
    pub fn ready(&self, scene: &Scene) -> bool {
        scene
            .layers
            .iter()
            .filter_map(|l| l.key.as_ref())
            .all(|key| self.textures.contains_key(key))
    }
    pub fn clear(&mut self) {
        self.textures.clear();
        self.failed.clear();
        let mut s = self.shared.0.lock().unwrap();
        s.wanted.clear();
        let discarded: Vec<Key> = s.queue.drain(..).collect();
        for key in discarded {
            s.pending.remove(&key);
        }
    }
    pub fn paint(&self, painter: &egui::Painter, rect: Rect, canvas: Vec2, scene: &Scene) {
        let scale = rect.width() / canvas.x;
        let p = |v: [f32; 2]| Pos2::new(rect.left() + v[0] * scale, rect.top() + v[1] * scale);
        let painter = painter.with_clip_rect(rect);
        painter.rect_filled(rect, 0., Color32::BLACK);
        for layer in &scene.layers {
            let [x, y, w, h] = layer.rect;
            let center = [x + w / 2., y + h / 2.];
            let (c, s) = (layer.angle.cos(), layer.angle.sin());
            let points: [[f32; 2]; 4] = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]].map(|v| {
                let (dx, dy) = (v[0] - center[0], v[1] - center[1]);
                [center[0] + dx * c - dy * s, center[1] + dx * s + dy * c]
            });
            if let Some(key) = &layer.key {
                if let Some(texture) = self.textures.get(key) {
                    let mut mesh = egui::Mesh::with_texture(texture.handle.id());
                    let [a, b, c, d] = layer.uv;
                    let uvs = [[a, b], [c, b], [c, d], [a, d]];
                    for (i, point) in points.iter().enumerate() {
                        mesh.vertices.push(egui::epaint::Vertex {
                            pos: p(*point),
                            uv: Pos2::new(uvs[i][0], uvs[i][1]),
                            color: Color32::from_white_alpha(
                                (layer.alpha.clamp(0., 1.) * 255.).round() as u8,
                            ),
                        });
                    }
                    mesh.indices = vec![0, 1, 2, 0, 2, 3];
                    painter.add(egui::Shape::mesh(mesh));
                }
            } else {
                painter.add(egui::Shape::convex_polygon(
                    points.iter().map(|v| p(*v)).collect(),
                    Color32::from_rgba_unmultiplied(0, 0, 0, (layer.alpha * 255.) as u8),
                    Stroke::NONE,
                ));
            }
            if layer.border > 0. {
                painter.add(egui::Shape::closed_line(
                    points.iter().map(|v| p(*v)).collect(),
                    Stroke::new(layer.border * scale, layer.border_color),
                ));
            }
        }
        for mark in &scene.marks {
            match mark {
                Mark::Fill([x, y, w, h], c) => {
                    painter.rect_filled(
                        Rect::from_min_size(p([*x, *y]), Vec2::new(*w * scale, *h * scale))
                            .expand(1. / painter.ctx().pixels_per_point()),
                        0.,
                        *c,
                    );
                }
                Mark::Line(points, c, w, closed) => {
                    let pts = points.iter().map(|v| p(*v)).collect();
                    painter.add(if *closed {
                        egui::Shape::closed_line(pts, Stroke::new(*w * scale, *c))
                    } else {
                        egui::Shape::line(pts, Stroke::new(*w * scale, *c))
                    });
                }
            }
        }
    }
}
impl Drop for Cache {
    fn drop(&mut self) {
        let mut s = self.shared.0.lock().unwrap();
        s.stop = true;
        s.wanted.clear();
        s.queue.clear();
        self.shared.1.notify_all();
    }
}
fn decode(key: &Key, state: &Arc<(Mutex<Shared>, Condvar)>) -> Result<Vec<u8>, String> {
    let size = key
        .width
        .checked_mul(key.height)
        .and_then(|v| v.checked_mul(4))
        .filter(|bytes| *bytes > 0 && *bytes <= MAX_CACHE_BYTES)
        .ok_or("Invalid source-frame dimensions")?;
    if key.width > 960 || key.height > 640 {
        return Err("Invalid source-frame dimensions".into());
    }
    if {
        let s = state.0.lock().unwrap();
        s.stop || !s.wanted.contains(key)
    } {
        return Err("Superseded".into());
    }
    let file = key.path.canonicalize().map_err(|e| e.to_string())?;
    let root = key.root.canonicalize().map_err(|e| e.to_string())?;
    if !file.starts_with(root) || !file.is_file() {
        return Err("Source must remain inside the recording folder".into());
    }
    let mut child = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-threads",
            "2",
            "-ss",
            &format!("{:.6}", key.time_us as f64 / 1e6),
            "-i",
        ])
        .arg(file)
        .args([
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            &format!(
                "scale=w=\'min(iw,{})\':h=\'min(ih,{})\':flags=bilinear",
                key.width, key.height
            ),
            "-pix_fmt",
            "rgba",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    let mut output = child.stdout.take().unwrap();
    let fd = output.as_raw_fd();
    let nonblocking = unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        flags >= 0 && libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) >= 0
    };
    if !nonblocking {
        let _ = child.kill();
        let _ = child.wait();
        return Err("Could not configure cancellable source decoding".into());
    }
    let mut bytes = vec![0; size];
    let mut read = 0;
    let started = Instant::now();
    let result = loop {
        if started.elapsed() > Duration::from_secs(5) {
            break Err("Source-frame decoding timed out".into());
        }
        if {
            let s = state.0.lock().unwrap();
            s.stop || !s.wanted.contains(key)
        } {
            break Err("Superseded".into());
        }
        match output.read(&mut bytes[read..]) {
            Ok(0) => break Err("No source frame was decoded".into()),
            Ok(n) => {
                read += n;
                if read == bytes.len() {
                    break Ok(bytes);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(2))
            }
            Err(e) => break Err(e.to_string()),
        }
    };
    let _ = child.kill();
    let _ = child.wait();
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture() -> (Value, Value) {
        let project = json!({"sources":[{"id":"v","file":"media/v.mkv","type":"video","width":1920,"height":1080,"media_duration":10}]});
        let model = json!({"width":1920,"height":1080,"fps":60,"tracks":[{"kind":"video","clips":[{"id":"c","source_id":"v","start":0,"in":1,"length":4,"x":0,"y":0,"w":1920,"h":1080}]}],"annotations":[]});
        (project, model)
    }
    #[test]
    fn geometry_edits_keep_the_decoded_frame_key() {
        let (p, mut m) = fixture();
        let a = scene(Path::new("/recording/project.katman"), &p, &m, 1.).unwrap();
        m["tracks"][0]["clips"][0]["x"] = json!(500);
        m["tracks"][0]["clips"][0]["w"] = json!(800);
        let b = scene(Path::new("/recording/project.katman"), &p, &m, 1.).unwrap();
        assert_eq!(a.layers[0].key, b.layers[0].key);
        assert_ne!(a.layers[0].rect, b.layers[0].rect);
    }
    #[test]
    fn unsupported_privacy_and_color_effects_never_get_skipped() {
        let (p, mut m) = fixture();
        m["annotations"] = json!([{"kind":"blur","start":0,"end":3}]);
        assert!(scene(Path::new("/r/project.katman"), &p, &m, 1.).is_none());
        m["annotations"] = json!([]);
        m["tracks"][0]["clips"][0]["gamma"] = json!(1.2);
        assert!(scene(Path::new("/r/project.katman"), &p, &m, 1.).is_none());
    }
    #[test]
    fn crop_flip_and_redaction_are_immediate_geometry() {
        let (p, mut m) = fixture();
        m["tracks"][0]["clips"][0]["crop_w"] = json!(0.5);
        m["tracks"][0]["clips"][0]["flip_x"] = json!(true);
        m["annotations"] = json!([{"kind":"redact","start":0,"end":3,"x":10,"y":20,"w":30,"h":40}]);
        let s = scene(Path::new("/r/project.katman"), &p, &m, 1.).unwrap();
        assert_eq!(s.layers[0].uv, [0.5, 0., 0., 1.]);
        assert_eq!(s.marks.len(), 1);
    }
    #[test]
    fn paths_and_time_windows_are_checked() {
        let (mut p, m) = fixture();
        p["sources"][0]["file"] = json!("../outside.mkv");
        assert!(scene(Path::new("/r/project.katman"), &p, &m, 1.).is_none());
        let (p, m) = fixture();
        assert!(
            scene(Path::new("/r/project.katman"), &p, &m, 8.)
                .unwrap()
                .layers
                .is_empty()
        );
    }
    fn media() -> (tempfile::TempDir, Key) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("colors.mkv");
        let result = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=64x48:r=10:d=1",
                "-f",
                "lavfi",
                "-i",
                "color=blue:s=64x48:r=10:d=1",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0",
                "-c:v",
                "ffv1",
                "-threads",
                "1",
            ])
            .arg(&path)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let key = Key {
            path,
            root: dir.path().into(),
            time_us: 1_200_000,
            width: 64,
            height: 48,
        };
        (dir, key)
    }
    fn state_for(key: &Key) -> Arc<(Mutex<Shared>, Condvar)> {
        let mut shared = Shared::default();
        shared.wanted.insert(key.clone());
        Arc::new((Mutex::new(shared), Condvar::new()))
    }
    fn scene_for(key: Key) -> Scene {
        Scene {
            layers: vec![Layer {
                key: Some(key),
                rect: [0., 0., 64., 48.],
                uv: [0., 0., 1., 1.],
                angle: 0.,
                alpha: 1.,
                border: 0.,
                border_color: Color32::WHITE,
            }],
            marks: vec![],
        }
    }
    #[test]
    fn real_decoder_seeks_rgba_and_rejects_upscale_or_invalid_allocations() {
        let (_dir, mut key) = media();
        let bytes = decode(&key, &state_for(&key)).unwrap();
        assert_eq!(bytes.len(), 64 * 48 * 4);
        assert!(
            bytes[2] > 240 && bytes[0] < 10 && bytes[3] == 255,
            "seek must select blue second"
        );
        key.width = 128; // Actual source is 64px: do not upscale it.
        assert!(decode(&key, &state_for(&key)).is_err());
        key.width = usize::MAX;
        assert!(
            decode(&key, &state_for(&key))
                .unwrap_err()
                .contains("dimensions")
        );
    }
    #[test]
    fn real_decode_can_be_superseded_without_poisoning_future_requests() {
        let (_dir, key) = media();
        let state = state_for(&key);
        let decode_state = state.clone();
        let decode_key = key.clone();
        let started = Instant::now();
        let worker = thread::spawn(move || decode(&decode_key, &decode_state));
        thread::sleep(Duration::from_millis(3));
        state.0.lock().unwrap().wanted.clear();
        assert_eq!(worker.join().unwrap().unwrap_err(), "Superseded");
        assert!(started.elapsed() < Duration::from_secs(1));
        state.0.lock().unwrap().wanted.insert(key.clone());
        assert!(decode(&key, &state).is_ok());
    }
    #[test]
    fn cache_reuses_real_frames_and_clear_does_not_duplicate_pending_decode() {
        let (_dir, key) = media();
        let ctx = egui::Context::default();
        let scene = scene_for(key);
        let mut cache = Cache::new();
        cache.request(&scene);
        let started = Instant::now();
        while !cache.ready(&scene)
            && !cache.failed(&scene)
            && started.elapsed() < Duration::from_secs(3)
        {
            cache.request(&scene);
            cache.poll(&ctx);
            thread::sleep(Duration::from_millis(2));
        }
        assert!(cache.ready(&scene));
        assert_eq!(cache.decoded, 1);
        for _ in 0..100 {
            cache.request(&scene);
            cache.poll(&ctx);
        }
        assert_eq!(cache.decoded, 1);
        assert!(cache.shared.0.lock().unwrap().pending.is_empty());
        cache.clear();
        assert!(!cache.ready(&scene));
        cache.request(&scene);
        let started = Instant::now();
        while !cache.ready(&scene) && started.elapsed() < Duration::from_secs(3) {
            cache.poll(&ctx);
            thread::sleep(Duration::from_millis(2));
        }
        assert!(cache.ready(&scene));
        assert_eq!(cache.decoded, 2);
    }
    #[test]
    fn text_reverse_and_source_overrun_use_accurate_fallback() {
        let (p, mut m) = fixture();
        let path = Path::new("/r/project.katman");
        m["annotations"] =
            json!([{"kind":"text","start":0,"end":3,"font":"Noto Sans","text":"Actual font"}]);
        assert!(scene(path, &p, &m, 1.).is_none());
        m["annotations"] = json!([]);
        m["tracks"][0]["clips"][0]["reverse"] = json!(true);
        assert!(scene(path, &p, &m, 1.).is_none());
        m["tracks"][0]["clips"][0]["reverse"] = json!(false);
        m["tracks"][0]["clips"][0]["in"] = json!(10.);
        assert!(scene(path, &p, &m, 1.).is_none());
    }
    #[test]
    fn small_odd_sources_are_not_upscaled_and_fades_keep_effect_clock() {
        let (mut p, mut m) = fixture();
        p["sources"][0]["width"] = json!(3);
        p["sources"][0]["height"] = json!(1);
        m["tracks"][0]["clips"][0]["fade_in"] = json!(4.);
        m["tracks"][0]["clips"][0]["effect_offset"] = json!(1.);
        let s = scene(Path::new("/r/project.katman"), &p, &m, 1.).unwrap();
        let key = s.layers[0].key.as_ref().unwrap();
        assert_eq!((key.width, key.height), (3, 1));
        assert_eq!(s.layers[0].alpha, 0.5);
        m["tracks"][0]["clips"][0]["fade_out"] = json!(2.);
        m["tracks"][0]["clips"][0]["effect_length"] = json!(3.);
        assert_eq!(
            scene(Path::new("/r/project.katman"), &p, &m, 1.)
                .unwrap()
                .layers[0]
                .alpha,
            0.25
        );
    }
    #[test]
    fn cache_budget_is_hard_even_for_wanted_frames() {
        let ctx = egui::Context::default();
        let shared = Arc::new((Mutex::new(Shared::default()), Condvar::new()));
        let (tx, rx) = mpsc::sync_channel(1);
        let mut cache = Cache {
            shared,
            rx,
            textures: HashMap::new(),
            failed: HashSet::new(),
            clock: 0,
            decoded: 0,
            last_decode_ms: 0.,
        };
        for i in 0..30 {
            let key = Key {
                path: PathBuf::from(format!("/r/{i}")),
                root: PathBuf::from("/r"),
                time_us: 0,
                width: 960,
                height: 640,
            };
            cache.shared.0.lock().unwrap().wanted.insert(key.clone());
            tx.send(Pixels {
                key,
                rgba: Ok(vec![0; 960 * 640 * 4]),
                elapsed: Duration::ZERO,
            })
            .unwrap();
            cache.poll(&ctx);
            assert!(cache.textures.values().map(|t| t.bytes).sum::<usize>() <= MAX_CACHE_BYTES);
        }
        assert!(!cache.failed.is_empty());
        assert_eq!(cache.textures.len(), 27);
    }
    #[test]
    #[ignore = "manual CPU composition benchmark; run with --ignored --nocapture"]
    fn benchmark_cached_geometry_composition() {
        let (_dir, key) = media();
        let mut scene = scene_for(key.clone());
        scene.layers = vec![scene.layers[0].clone(); 8];
        for i in 0..16 {
            scene
                .marks
                .push(Mark::Fill([i as f32 * 2., 2., 2., 3.], Color32::BLACK));
        }
        let mut cache = Cache::new();
        let ctx = egui::Context::default();
        cache.request(&scene);
        let deadline = Instant::now();
        while !cache.ready(&scene) && deadline.elapsed() < Duration::from_secs(3) {
            cache.poll(&ctx);
            thread::sleep(Duration::from_millis(2));
        }
        assert!(cache.ready(&scene));
        let start = Instant::now();
        let frames = 1000;
        for _ in 0..frames {
            let output = ctx.run(egui::RawInput::default(), |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    cache.request(&scene);
                    cache.poll(ctx);
                    cache.paint(
                        ui.painter(),
                        Rect::from_min_size(Pos2::ZERO, Vec2::new(960., 640.)),
                        Vec2::new(64., 48.),
                        &scene,
                    );
                });
            });
            std::hint::black_box(ctx.tessellate(output.shapes, output.pixels_per_point));
        }
        eprintln!(
            "cached geometry CPU: {frames} frames, {:.3} ms/frame; decoded={} textures={} bytes={}",
            start.elapsed().as_secs_f64() * 1000. / frames as f64,
            cache.decoded,
            cache.textures.len(),
            cache.textures.values().map(|t| t.bytes).sum::<usize>()
        );
        assert_eq!(cache.decoded, 1);
    }
    #[test]
    fn oversized_working_set_falls_back_but_shared_frames_count_once() {
        let (mut p, mut m) = fixture();
        p["sources"][0]["width"] = json!(960);
        p["sources"][0]["height"] = json!(640);
        let clip = m["tracks"][0]["clips"][0].clone();
        let clips: Vec<Value> = (0..30)
            .map(|i| {
                let mut clip = clip.clone();
                clip["id"] = json!(format!("clip{i}"));
                clip["in"] = json!(i as f64 / 10.);
                clip
            })
            .collect();
        m["tracks"][0]["clips"] = json!(clips);
        assert!(scene(Path::new("/r/project.katman"), &p, &m, 1.).is_none());
        for clip in m["tracks"][0]["clips"].as_array_mut().unwrap() {
            clip["in"] = json!(0.);
        }
        assert_eq!(
            scene(Path::new("/r/project.katman"), &p, &m, 1.)
                .unwrap()
                .layers
                .len(),
            30
        );
    }
    #[test]
    fn decoder_rejects_symlink_escape_before_spawning() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        let link = root.path().join("source.mkv");
        std::os::unix::fs::symlink(outside.path(), &link).unwrap();
        let key = Key {
            path: link,
            root: root.path().into(),
            time_us: 0,
            width: 64,
            height: 48,
        };
        assert!(
            decode(&key, &state_for(&key))
                .unwrap_err()
                .contains("inside the recording folder")
        );
    }
    #[test]
    fn redaction_is_opaque_and_covers_original_geometry_at_fractional_scale() {
        let (p, mut m) = fixture();
        m["annotations"] = json!([{"kind":"redact","start":0,"end":3,"x":10.,"y":20.,"w":30.,"h":40.,"opacity":0.1}]);
        let mut scene = scene(Path::new("/r/project.katman"), &p, &m, 1.).unwrap();
        scene.layers.clear();
        assert!(matches!(scene.marks[0], Mark::Fill(_, Color32::BLACK)));
        let cache = Cache::new();
        let ctx = egui::Context::default();
        let output = ctx.run(egui::RawInput::default(), |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| {
                cache.paint(
                    ui.painter(),
                    Rect::from_min_size(Pos2::ZERO, Vec2::new(960., 540.)),
                    Vec2::new(1920., 1080.),
                    &scene,
                )
            });
        });
        let mask = output
            .shapes
            .iter()
            .filter_map(|shape| {
                if let egui::Shape::Rect(rect) = &shape.shape {
                    Some(rect)
                } else {
                    None
                }
            })
            .find(|rect| rect.rect.width() < 100.)
            .unwrap();
        assert!(mask.rect.left() < 5. && mask.rect.right() > 20.);
        assert!(mask.rect.top() < 10. && mask.rect.bottom() > 30.);
        assert_eq!(mask.fill, Color32::BLACK);
    }
}
