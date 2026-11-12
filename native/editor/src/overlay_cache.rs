// SPDX-License-Identifier: GPL-3.0-only
//! Independent title/caption sprites, with bounded memory and priority for the
//! visible scene. Upcoming captions are optional, cancellable prefetch work.
use eframe::egui;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    io::{Read, Write},
    os::{fd::AsRawFd, unix::process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Condvar, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};
const MAX_BYTES: usize = 64 * 1024 * 1024;
const MAX_TITLE_BYTES: usize = 32 * 1024 * 1024;
const MAX_KEYS: usize = 64;
const MAX_TEXTURES: usize = 256;
const SUPERSEDED: &str = "Superseded";
#[derive(Clone, Debug, Hash, Eq, PartialEq)]
pub struct TextKey {
    pub font: String,
    pub text: String,
    pub size_px: u32,
    pub width: u32,
    pub height: u32,
}
impl TextKey {
    fn bytes(&self) -> Option<usize> {
        if !(4..=4096).contains(&self.width)
            || !(4..=2048).contains(&self.height)
            || !(1..=1000).contains(&self.size_px)
            || self.text.len() > 64 * 1024
            || self.text.contains('\0')
            || self.font.trim().is_empty()
            || self.font.len() > 1024
            || self.font.chars().any(|c| c < ' ')
        {
            return None;
        }
        (self.width as usize)
            .checked_mul(self.height as usize)?
            .checked_mul(4)
            .filter(|bytes| *bytes <= MAX_BYTES)
    }
    fn request(&self) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "font":self.font,"text":self.text,"size_px":self.size_px,
            "width":self.width,"height":self.height,
        }))
        .expect("Text request is JSON")
    }
}

#[derive(Clone, Debug, Hash, Eq, PartialEq)]
pub struct CaptionKey {
    pub canvas: [u32; 2],
    pub raster: [u32; 2],
    pub captions: Vec<Value>,
    pub style: Value,
}
impl CaptionKey {
    fn bytes(&self) -> Option<usize> {
        if self.canvas.iter().any(|v| !(2..=32768).contains(v))
            || !(2..=3840).contains(&self.raster[0])
            || !(2..=2160).contains(&self.raster[1])
            || self.captions.len() > 128
            || !self.style.is_object()
        {
            return None;
        }
        if self.captions.iter().any(|c| {
            !c.is_object()
                || !c["text"].is_string()
                || c["text"]
                    .as_str()
                    .is_some_and(|s| s.len() > 64 * 1024 || s.contains('\0'))
        }) {
            return None;
        }
        let bytes = self.raster[0] as usize * self.raster[1] as usize * 4;
        (bytes <= 32 * 1024 * 1024 && self.request().len() <= 512 * 1024).then_some(bytes)
    }
    fn request(&self) -> Vec<u8> {
        serde_json::to_vec(&json!({"canvas":self.canvas,"width":self.raster[0],"height":self.raster[1],"captions":self.captions,"style":self.style})).expect("Caption JSON")
    }
}
#[derive(Clone, Debug, Hash, Eq, PartialEq)]
enum Key {
    Title(TextKey),
    Caption(CaptionKey),
}
impl Key {
    fn bytes(&self) -> Option<usize> {
        match self {
            Self::Title(k) => k.bytes(),
            Self::Caption(k) => k.bytes(),
        }
    }
    fn request(&self) -> Vec<u8> {
        match self {
            Self::Title(k) => k.request(),
            Self::Caption(k) => k.request(),
        }
    }
    fn module(&self) -> &'static str {
        match self {
            Self::Title(_) => "katman.overlay_raster",
            Self::Caption(_) => "katman.caption_raster",
        }
    }
    fn bounds(&self) -> [u32; 2] {
        match self {
            Self::Title(k) => [k.width, k.height],
            Self::Caption(k) => k.raster,
        }
    }
}
#[derive(Default)]
struct Shared {
    wanted: HashSet<Key>,
    required: HashSet<Key>,
    pending: HashSet<Key>,
    queue: VecDeque<Key>,
    stop: bool,
}
type State = Arc<(Mutex<Shared>, Condvar)>;
struct Raster {
    rgba: Vec<u8>,
    bounds: [u32; 4],
}
struct Pixels {
    key: Key,
    result: Result<Raster, String>,
}
struct Texture {
    handle: egui::TextureHandle,
    used: u64,
    bytes: usize,
    bounds: [u32; 4],
}
pub struct Cache {
    shared: State,
    rx: mpsc::Receiver<Pixels>,
    textures: HashMap<Key, Texture>,
    failures: HashSet<Key>,
    clock: u64,
    pub rasterized: u64,
}
impl Cache {
    pub fn new(root: PathBuf) -> Self {
        let shared = Arc::new((Mutex::new(Shared::default()), Condvar::new()));
        let (tx, rx) = mpsc::sync_channel(2);
        let state = shared.clone();
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
                let result = rasterize_image(&root, &key, &state);
                if tx.send(Pixels { key, result }).is_err() {
                    return;
                }
            }
        });
        Self {
            shared,
            rx,
            textures: HashMap::new(),
            failures: HashSet::new(),
            clock: 0,
            rasterized: 0,
        }
    }
    pub fn request(&mut self, keys: &[TextKey]) {
        self.request_layers(keys, &[], &[]);
    }
    pub fn request_layers(
        &mut self,
        titles: &[TextKey],
        captions: &[CaptionKey],
        upcoming: &[CaptionKey],
    ) {
        self.clock = self.clock.saturating_add(1);
        let required: Vec<Key> = titles
            .iter()
            .cloned()
            .map(Key::Title)
            .chain(captions.iter().cloned().map(Key::Caption))
            .collect();
        let required_set: HashSet<Key> = required.iter().cloned().collect();
        let keys: Vec<Key> = required
            .into_iter()
            .chain(upcoming.iter().cloned().map(Key::Caption))
            .collect();
        let mut wanted = HashSet::new();
        let mut ordered = vec![];
        let mut bytes = 0usize;
        let mut title_bytes = 0usize;
        for key in keys {
            if wanted.contains(&key) {
                continue;
            }
            let size = key
                .bytes()
                .map(|max| self.textures.get(&key).map_or(max, |t| t.bytes));
            let title = matches!(key, Key::Title(_));
            if wanted.len() >= MAX_KEYS
                || size.is_none()
                || bytes.saturating_add(size.unwrap_or(0)) > MAX_BYTES
                || (title && title_bytes.saturating_add(size.unwrap_or(0)) > MAX_TITLE_BYTES)
            {
                if required_set.contains(&key) {
                    self.failures.insert(key);
                }
                continue;
            }
            bytes += size.unwrap();
            if title {
                title_bytes += size.unwrap();
            }
            if let Some(texture) = self.textures.get_mut(&key) {
                texture.used = self.clock;
            }
            wanted.insert(key.clone());
            ordered.push(key);
        }
        let mut s = self.shared.0.lock().unwrap();
        let discarded: Vec<Key> = s
            .queue
            .iter()
            .filter(|k| !wanted.contains(*k))
            .cloned()
            .collect();
        s.queue.retain(|k| wanted.contains(k));
        for key in discarded {
            s.pending.remove(&key);
        }
        s.wanted = wanted;
        s.required = required_set;
        // Visible requests precede prefetch, including requests already queued.
        for key in ordered {
            if !self.textures.contains_key(&key)
                && !self.failures.contains(&key)
                && s.pending.insert(key.clone())
            {
                s.queue.push_back(key);
            }
        }
        let mut required_queue = VecDeque::new();
        let mut optional_queue = VecDeque::new();
        while let Some(key) = s.queue.pop_front() {
            if s.required.contains(&key) {
                required_queue.push_back(key)
            } else {
                optional_queue.push_back(key)
            }
        }
        required_queue.append(&mut optional_queue);
        s.queue = required_queue;
        if self.failures.len() > MAX_KEYS * 2 {
            self.failures.retain(|k| s.required.contains(k));
        }
        self.shared.1.notify_one();
    }
    fn upload_fits(&self, key: &Key, bytes: usize) -> bool {
        let replaced = self.textures.get(key).map_or(0, |texture| texture.bytes);
        let retained = self
            .textures
            .values()
            .map(|texture| texture.bytes)
            .sum::<usize>();
        retained.saturating_sub(replaced).saturating_add(bytes) <= MAX_BYTES
            && self.textures.len() + usize::from(!self.textures.contains_key(key)) <= MAX_TEXTURES
    }
    pub fn poll(&mut self, ctx: &egui::Context) {
        for pixels in self.rx.try_iter() {
            let (required, wanted) = {
                let mut s = self.shared.0.lock().unwrap();
                s.pending.remove(&pixels.key);
                (
                    s.required.contains(&pixels.key),
                    s.wanted.contains(&pixels.key),
                )
            };
            if !wanted {
                continue;
            }
            match pixels.result {
                Ok(raster) => {
                    let bytes = raster.rgba.len();
                    let needed = self.shared.0.lock().unwrap().required.clone();
                    while !self.upload_fits(&pixels.key, bytes) {
                        let old = self
                            .textures
                            .iter()
                            .filter(|(key, _)| !needed.contains(*key) && *key != &pixels.key)
                            .min_by_key(|(_, t)| t.used)
                            .map(|(k, _)| k.clone());
                        if let Some(old) = old {
                            self.textures.remove(&old);
                        } else {
                            break;
                        }
                    }
                    if !self.upload_fits(&pixels.key, bytes) {
                        if required {
                            self.failures.insert(pixels.key);
                        }
                        continue;
                    }
                    let image = egui::ColorImage::from_rgba_unmultiplied(
                        [raster.bounds[2] as usize, raster.bounds[3] as usize],
                        &raster.rgba,
                    );
                    let handle =
                        ctx.load_texture("Text overlay", image, egui::TextureOptions::LINEAR);
                    self.rasterized += 1;
                    self.textures.insert(
                        pixels.key,
                        Texture {
                            handle,
                            used: self.clock,
                            bytes,
                            bounds: raster.bounds,
                        },
                    );
                }
                Err(reason) if reason == SUPERSEDED => {
                    let mut s = self.shared.0.lock().unwrap();
                    if s.wanted.contains(&pixels.key) && s.pending.insert(pixels.key.clone()) {
                        s.queue.push_back(pixels.key);
                        self.shared.1.notify_one();
                    }
                }
                Err(_) => {
                    self.failures.insert(pixels.key);
                }
            }
        }
    }
    pub fn texture(&self, key: &TextKey) -> Option<egui::TextureId> {
        self.textures
            .get(&Key::Title(key.clone()))
            .map(|t| t.handle.id())
    }
    pub fn ready(&self, key: &TextKey) -> bool {
        self.textures.contains_key(&Key::Title(key.clone()))
    }
    pub fn failed(&self, key: &TextKey) -> bool {
        self.failed_key(&Key::Title(key.clone()))
    }
    pub fn known_failure(&self, key: &TextKey) -> bool {
        let key = Key::Title(key.clone());
        self.failures.contains(&key) || key.bytes().is_none()
    }
    pub fn known_caption_failure(&self, key: &CaptionKey) -> bool {
        let key = Key::Caption(key.clone());
        self.failures.contains(&key) || key.bytes().is_none()
    }
    pub fn caption_sprite(&self, key: &CaptionKey) -> Option<(egui::TextureId, [u32; 4])> {
        self.textures
            .get(&Key::Caption(key.clone()))
            .map(|t| (t.handle.id(), t.bounds))
    }
    pub fn caption_ready(&self, key: &CaptionKey) -> bool {
        self.textures.contains_key(&Key::Caption(key.clone()))
    }
    pub fn caption_failed(&self, key: &CaptionKey) -> bool {
        self.failed_key(&Key::Caption(key.clone()))
    }
    fn failed_key(&self, key: &Key) -> bool {
        self.failures.contains(key)
            || key.bytes().is_none()
            || !self.shared.0.lock().unwrap().wanted.contains(key)
    }
    pub fn clear(&mut self) {
        self.textures.clear();
        self.failures.clear();
        let mut s = self.shared.0.lock().unwrap();
        s.wanted.clear();
        s.required.clear();
        let discarded: Vec<Key> = s.queue.drain(..).collect();
        for key in discarded {
            s.pending.remove(&key);
        }
    }
}
impl Drop for Cache {
    fn drop(&mut self) {
        let mut s = self.shared.0.lock().unwrap();
        s.stop = true;
        s.wanted.clear();
        s.required.clear();
        s.queue.clear();
        self.shared.1.notify_one();
    }
}
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.wait();
    }
}
fn cancelled(state: &State, key: &Key) -> bool {
    let s = state.0.lock().unwrap();
    s.stop
        || !s.wanted.contains(key)
        || (!s.required.contains(key) && s.queue.iter().any(|k| s.required.contains(k)))
}
fn nonblocking(fd: i32) -> bool {
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        flags >= 0 && libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) >= 0
    }
}
/// Validate cropped headers before allocating their payload. The worker cannot
/// place pixels outside the requested canvas or grow a declared allocation.
fn caption_header(data: &[u8], limit: [u32; 2]) -> Result<[u32; 4], String> {
    let value: Value = serde_json::from_slice(data).map_err(|_| "Invalid caption header")?;
    let mut rect = [0; 4];
    for (i, name) in ["x", "y", "width", "height"].iter().enumerate() {
        rect[i] = value[*name]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or("Invalid caption bounds")?;
    }
    if rect[2] == 0
        || rect[3] == 0
        || rect[0].checked_add(rect[2]).is_none_or(|n| n > limit[0])
        || rect[1].checked_add(rect[3]).is_none_or(|n| n > limit[1])
    {
        return Err("Caption exceeds its raster bounds".into());
    }
    Ok(rect)
}
fn rasterize_image(root: &Path, key: &Key, state: &State) -> Result<Raster, String> {
    let max = key.bytes().ok_or("Invalid text overlay request")?;
    if cancelled(state, key) {
        return Err(SUPERSEDED.into());
    }
    let python = std::env::var("LAYER_PYTHON").unwrap_or_else(|_| "python3".into());
    let child = Command::new(python)
        .args(["-m", key.module()])
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map_err(|e| e.to_string())?;
    let mut process = Process(child);
    let mut input = process.0.stdin.take();
    let mut output = process.0.stdout.take().ok_or("Missing overlay output")?;
    if !nonblocking(output.as_raw_fd())
        || !input.as_ref().is_some_and(|i| nonblocking(i.as_raw_fd()))
    {
        return Err("Could not configure cancellable overlay rasterization".into());
    }
    let request = key.request();
    let mut written = 0;
    let mut data = Vec::new();
    let mut header = None;
    let mut expected = if matches!(key, Key::Title(_)) {
        Some(max)
    } else {
        None
    };
    let started = Instant::now();
    let mut buffer = [0u8; 16384];
    loop {
        if cancelled(state, key) {
            return Err(SUPERSEDED.into());
        }
        if started.elapsed() > Duration::from_secs(10) {
            return Err("Text overlay rasterization timed out".into());
        }
        if let Some(stdin) = input.as_mut() {
            match stdin.write(&request[written..]) {
                Ok(0) => return Err("Overlay closed its input".into()),
                Ok(n) => {
                    written += n;
                    if written == request.len() {
                        input = None;
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.to_string()),
            }
        }
        let count = expected.map_or(buffer.len(), |n| (n - data.len()).min(buffer.len()));
        match output.read(&mut buffer[..count]) {
            Ok(0) => return Err("Overlay returned an incomplete sprite".into()),
            Ok(n) => {
                data.extend_from_slice(&buffer[..n]);
                if expected.is_none() {
                    if let Some(end) = data.iter().position(|b| *b == b'\n') {
                        if end > 256 {
                            return Err("Caption header is too large".into());
                        }
                        let bounds = caption_header(&data[..end], key.bounds())?;
                        data.drain(..=end);
                        expected = Some(bounds[2] as usize * bounds[3] as usize * 4);
                        header = Some(bounds);
                    } else if data.len() > 256 {
                        return Err("Caption header is too large".into());
                    }
                }
                if let Some(bytes) = expected {
                    if data.len() > bytes {
                        return Err("Overlay returned an oversized sprite".into());
                    }
                    if data.len() == bytes {
                        return Ok(Raster {
                            rgba: data,
                            bounds: header.unwrap_or([0, 0, key.bounds()[0], key.bounds()[1]]),
                        });
                    }
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                ) => {}
            Err(e) => return Err(e.to_string()),
        }
        thread::sleep(Duration::from_millis(2));
    }
}
#[cfg(test)]
fn rasterize(root: &Path, key: &TextKey, state: &State) -> Result<Vec<u8>, String> {
    rasterize_image(root, &Key::Title(key.clone()), state).map(|r| r.rgba)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    }
    fn key(text: &str) -> TextKey {
        TextKey {
            font: "Noto Sans".into(),
            text: text.into(),
            size_px: 24,
            width: 257,
            height: 96,
        }
    }
    fn state_for(key: &TextKey) -> State {
        let mut state = Shared::default();
        state.wanted.insert(Key::Title(key.clone()));
        Arc::new((Mutex::new(state), Condvar::new()))
    }
    fn wait(cache: &mut Cache, ctx: &egui::Context, key: &TextKey) {
        let start = Instant::now();
        while !cache.ready(key) && !cache.failed(key) && start.elapsed() < Duration::from_secs(4) {
            cache.poll(ctx);
            thread::sleep(Duration::from_millis(2));
        }
        assert!(cache.ready(key), "sprite failed or timed out");
    }
    #[test]
    fn real_rasterizer_outputs_transparent_odd_sized_sprite() {
        let key = key("Title Şğ\nSecond line");
        let rgba = rasterize(&root(), &key, &state_for(&key)).unwrap();
        assert_eq!(rgba.len(), 257 * 96 * 4);
        assert_eq!(*rgba.last().unwrap(), 0);
        assert!(
            rgba.chunks_exact(4)
                .any(|pixel| pixel == [255, 255, 255, 255])
        );
        assert!(rgba.chunks_exact(4).any(|pixel| pixel == [0, 0, 0, 255]));
    }
    #[test]
    fn repeated_requests_reuse_texture_and_latest_text_supersedes_old() {
        let ctx = egui::Context::default();
        let mut cache = Cache::new(root());
        let old = key("Old");
        let new = key("Latest");
        cache.request(&[old]);
        cache.request(std::slice::from_ref(&new));
        wait(&mut cache, &ctx, &new);
        let texture = cache.texture(&new).unwrap();
        for _ in 0..100 {
            cache.request(std::slice::from_ref(&new));
            cache.poll(&ctx);
        }
        assert_eq!(cache.texture(&new), Some(texture));
        assert!(cache.shared.0.lock().unwrap().pending.is_empty());
        assert_eq!(cache.textures.len(), 1);
        assert_eq!(
            cache.textures.values().map(|t| t.bytes).sum::<usize>(),
            257 * 96 * 4
        );
        cache.clear();
        assert!(!cache.ready(&new));
        cache.request(std::slice::from_ref(&new));
        wait(&mut cache, &ctx, &new);
    }
    #[test]
    fn cancellation_kills_worker_group_and_allows_retry() {
        let key = key("Cancelled");
        let state = state_for(&key);
        let thread_state = state.clone();
        let thread_key = key.clone();
        let worker = thread::spawn(move || rasterize(&root(), &thread_key, &thread_state));
        thread::sleep(Duration::from_millis(3));
        state.0.lock().unwrap().wanted.clear();
        assert_eq!(worker.join().unwrap().unwrap_err(), SUPERSEDED);
        state
            .0
            .lock()
            .unwrap()
            .wanted
            .insert(Key::Title(key.clone()));
        assert!(rasterize(&root(), &key, &state).is_ok());
    }
    #[test]
    fn invalid_and_over_budget_requests_do_not_queue_unbounded_work() {
        let (_tx, rx) = mpsc::channel();
        let shared = Arc::new((Mutex::new(Shared::default()), Condvar::new()));
        let mut cache = Cache {
            shared,
            rx,
            textures: HashMap::new(),
            failures: HashSet::new(),
            clock: 0,
            rasterized: 0,
        };
        let mut first = key("First");
        first.width = 4096;
        first.height = 2048;
        let mut second = first.clone();
        second.text = "Second".into();
        cache.request(&[first.clone(), second.clone()]);
        assert!(cache.failed(&second));
        assert_eq!(cache.shared.0.lock().unwrap().queue.len(), 1);
        let mut invalid = key("Invalid");
        invalid.width = u32::MAX;
        cache.request(std::slice::from_ref(&invalid));
        assert!(cache.failed(&invalid));
        assert!(cache.shared.0.lock().unwrap().queue.is_empty());
        assert!(rasterize(&root(), &invalid, &state_for(&invalid)).is_err());
    }
}
#[cfg(test)]
mod caption_tests {
    use super::*;

    fn root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    }
    fn key(text: &str) -> CaptionKey {
        CaptionKey {
            canvas: [400, 240],
            raster: [400, 240],
            captions: vec![
                json!({"text":text,"style":{"font":"Noto Sans","size":26,"color":"#ffca70"}}),
            ],
            style: json!({"font":"Noto Sans","size":24,"color":"#ffffff","margin_left":20,"margin_right":20,"margin_bottom":20}),
        }
    }
    fn state_for(key: &CaptionKey) -> State {
        let mut state = Shared::default();
        state.wanted.insert(Key::Caption(key.clone()));
        state.required.insert(Key::Caption(key.clone()));
        Arc::new((Mutex::new(state), Condvar::new()))
    }
    fn idle_cache() -> Cache {
        let (_tx, rx) = mpsc::channel();
        Cache {
            shared: Arc::new((Mutex::new(Shared::default()), Condvar::new())),
            rx,
            textures: HashMap::new(),
            failures: HashSet::new(),
            clock: 0,
            rasterized: 0,
        }
    }
    fn wait(cache: &mut Cache, ctx: &egui::Context, key: &CaptionKey) {
        let started = Instant::now();
        while !cache.caption_ready(key)
            && !cache.caption_failed(key)
            && started.elapsed() < Duration::from_secs(5)
        {
            cache.poll(ctx);
            thread::sleep(Duration::from_millis(2));
        }
        assert!(cache.caption_ready(key), "caption failed or timed out");
    }

    #[test]
    fn real_libass_sprite_is_cropped_and_cache_reuses_it_for_100_requests() {
        let key = key("Cropped caption Şğ");
        let raster = rasterize_image(&root(), &Key::Caption(key.clone()), &state_for(&key))
            .expect("real Python/libass caption");
        let [x, y, width, height] = raster.bounds;
        assert!(x > 0 && y > 120, "caption must retain its canvas position");
        assert!(width < 400 && height < 120, "payload must be cropped");
        assert!(x + width <= 400 && y + height <= 240);
        assert_eq!(raster.rgba.len(), width as usize * height as usize * 4);
        assert!(raster.rgba.chunks_exact(4).any(|p| p[3] == 255));
        assert!(raster.rgba.chunks_exact(4).any(|p| p[3] == 0));

        let ctx = egui::Context::default();
        let mut cache = Cache::new(root());
        cache.request_layers(&[], std::slice::from_ref(&key), &[]);
        wait(&mut cache, &ctx, &key);
        let (texture, bounds) = cache.caption_sprite(&key).unwrap();
        assert_eq!(bounds, raster.bounds);
        assert_eq!(cache.rasterized, 1);
        for _ in 0..100 {
            cache.request_layers(&[], std::slice::from_ref(&key), &[]);
            cache.poll(&ctx);
        }
        assert_eq!(cache.caption_sprite(&key), Some((texture, bounds)));
        assert_eq!(cache.rasterized, 1);
        assert!(cache.shared.0.lock().unwrap().pending.is_empty());
        assert_eq!(cache.textures[&Key::Caption(key)].bytes, raster.rgba.len());
    }

    #[test]
    fn tiny_caption_rasters_have_a_retained_entry_cap_and_preserve_visible_sprites() {
        // Drive the completion channel directly: this tests retention/LRU, not
        // a background worker, font availability or rasterization timing.
        let (tx, rx) = mpsc::channel();
        let mut cache = idle_cache();
        cache.rx = rx;
        let ctx = egui::Context::default();
        let anchors: Vec<CaptionKey> = (0..MAX_KEYS - 1)
            .map(|index| key(&format!("Visible anchor {index}")))
            .collect();
        cache.request_layers(&[], &anchors, &[]);
        for anchor in &anchors {
            let queued = cache.shared.0.lock().unwrap().queue.pop_front().unwrap();
            assert_eq!(queued, Key::Caption(anchor.clone()));
            tx.send(Pixels {
                key: queued,
                result: Ok(Raster {
                    rgba: vec![0; 4],
                    bounds: [0, 0, 1, 1],
                }),
            })
            .unwrap();
            cache.poll(&ctx);
        }
        let earliest = key("Tiny cropped caption 0");
        let mut current = earliest.clone();
        for index in 0..300 {
            current = key(&format!("Tiny cropped caption {index}"));
            let mut visible = anchors.clone();
            visible.push(current.clone());
            cache.request_layers(&[], &visible, &[]);
            cache.request_layers(&[], &visible, &[]);
            let queued = {
                let mut shared = cache.shared.0.lock().unwrap();
                assert_eq!(shared.queue.len(), 1);
                assert_eq!(shared.pending.len(), 1);
                shared.queue.pop_front().unwrap()
            };
            assert_eq!(queued, Key::Caption(current.clone()));
            tx.send(Pixels {
                key: queued,
                result: Ok(Raster {
                    rgba: vec![0; 4],
                    bounds: [0, 0, 1, 1],
                }),
            })
            .unwrap();
            cache.poll(&ctx);
            assert!(cache.textures.len() <= MAX_TEXTURES);
            assert!(cache.caption_ready(&current));
            assert!(anchors.iter().all(|anchor| cache.caption_ready(anchor)));
            let shared = cache.shared.0.lock().unwrap();
            assert!(shared.queue.is_empty());
            assert!(shared.pending.is_empty());
        }
        assert_eq!(cache.textures.len(), MAX_TEXTURES);
        assert!(
            !cache.caption_ready(&earliest),
            "old non-visible caption must be evicted"
        );
        assert_eq!(cache.rasterized, (MAX_KEYS - 1 + 300) as u64);
        assert_eq!(
            cache
                .textures
                .values()
                .map(|texture| texture.bytes)
                .sum::<usize>(),
            MAX_TEXTURES * 4
        );
        let uploads = cache.rasterized;
        let mut visible = anchors;
        visible.push(current.clone());
        for _ in 0..100 {
            cache.request_layers(&[], &visible, &[]);
            cache.poll(&ctx);
        }
        assert_eq!(cache.rasterized, uploads);
        assert!(cache.caption_ready(&current));
        {
            let shared = cache.shared.0.lock().unwrap();
            assert!(shared.queue.is_empty());
            assert!(shared.pending.is_empty());
            assert!(cache.failures.is_empty());
        }
        // Optional prefetch may evict stale retained entries, but must not
        // sacrifice any of the currently required foreground sprites.
        let upcoming = key("Optional tiny caption at entry capacity");
        cache.request_layers(&[], &visible[1..], std::slice::from_ref(&upcoming));
        let queued = cache.shared.0.lock().unwrap().queue.pop_front().unwrap();
        assert_eq!(queued, Key::Caption(upcoming.clone()));
        tx.send(Pixels {
            key: queued,
            result: Ok(Raster {
                rgba: vec![0; 4],
                bounds: [0, 0, 1, 1],
            }),
        })
        .unwrap();
        cache.poll(&ctx);
        assert_eq!(cache.textures.len(), MAX_TEXTURES);
        assert!(cache.caption_ready(&upcoming));
        assert!(
            visible[1..]
                .iter()
                .all(|key| cache.caption_ready(key) && !cache.caption_failed(key))
        );
        let mut transitioned = visible[1..].to_vec();
        transitioned.push(upcoming);
        cache.request_layers(&[], &transitioned, &[]);
        let shared = cache.shared.0.lock().unwrap();
        assert!(shared.queue.is_empty());
        assert!(shared.pending.is_empty());
        assert!(cache.failures.is_empty());
    }

    #[test]
    fn prefetched_caption_transition_uses_existing_sprite_without_rasterizing_again() {
        let current = key("First caption");
        let mut upcoming = key("Second caption\n100% {literal} \\N");
        upcoming.captions[0]["style"]["color"] = json!("#80ffb0");
        let ctx = egui::Context::default();
        let mut cache = Cache::new(root());
        cache.request_layers(
            &[],
            std::slice::from_ref(&current),
            std::slice::from_ref(&upcoming),
        );
        wait(&mut cache, &ctx, &current);
        wait(&mut cache, &ctx, &upcoming);
        assert_eq!(cache.rasterized, 2);
        let prefetched = cache.caption_sprite(&upcoming).unwrap();
        for _ in 0..100 {
            cache.request_layers(&[], std::slice::from_ref(&upcoming), &[]);
            cache.poll(&ctx);
        }
        assert_eq!(cache.caption_sprite(&upcoming), Some(prefetched));
        assert_eq!(cache.rasterized, 2);
        assert!(!cache.caption_failed(&upcoming));
    }

    #[test]
    fn prefetch_yields_to_required_work_and_required_queue_keeps_priority() {
        let current = key("Visible now");
        let upcoming = key("Optional future");
        let mut cache = idle_cache();
        cache.request_layers(&[], &[], std::slice::from_ref(&upcoming));
        // Simulate the single worker taking optional work, leaving it pending.
        assert_eq!(
            cache.shared.0.lock().unwrap().queue.pop_front(),
            Some(Key::Caption(upcoming.clone()))
        );
        cache.request_layers(
            &[],
            std::slice::from_ref(&current),
            std::slice::from_ref(&upcoming),
        );
        assert!(cancelled(&cache.shared, &Key::Caption(upcoming.clone())));
        assert!(!cancelled(&cache.shared, &Key::Caption(current.clone())));
        assert_eq!(
            cache.shared.0.lock().unwrap().queue.front(),
            Some(&Key::Caption(current.clone()))
        );
        let mut state = cache.shared.0.lock().unwrap();
        state.queue.clear();
        drop(state);
        assert!(!cancelled(&cache.shared, &Key::Caption(upcoming)));
    }

    #[test]
    fn caption_headers_reject_malformed_oversized_and_overflowed_bounds() {
        for raw in [
            "not-json",
            "[]",
            "{}",
            r#"{"x":-1,"y":0,"width":1,"height":1}"#,
            r#"{"x":0.5,"y":0,"width":1,"height":1}"#,
            r#"{"x":"0","y":0,"width":1,"height":1}"#,
            r#"{"x":true,"y":0,"width":1,"height":1}"#,
            r#"{"x":0,"y":0,"width":0,"height":1}"#,
            r#"{"x":0,"y":0,"width":1,"height":0}"#,
            r#"{"x":400,"y":0,"width":1,"height":1}"#,
            r#"{"x":0,"y":240,"width":1,"height":1}"#,
            r#"{"x":0,"y":0,"width":401,"height":240}"#,
            r#"{"x":4294967296,"y":0,"width":1,"height":1}"#,
            r#"{"x":18446744073709551615,"y":0,"width":1,"height":1}"#,
        ] {
            assert!(caption_header(raw.as_bytes(), [400, 240]).is_err(), "{raw}");
        }
        assert!(
            caption_header(
                br#"{"x":4294967295,"y":0,"width":2,"height":1}"#,
                [u32::MAX, u32::MAX]
            )
            .is_err()
        );
        assert_eq!(
            caption_header(br#"{"x":399,"y":239,"width":1,"height":1}"#, [400, 240]).unwrap(),
            [399, 239, 1, 1]
        );
        assert_eq!(
            caption_header(br#"{"x":0,"y":0,"width":400,"height":240}"#, [400, 240]).unwrap(),
            [0, 0, 400, 240]
        );
    }

    #[test]
    fn caption_protocol_rejects_long_headers_oversized_payloads_and_truncation() {
        let key = key("Protocol validation");
        let cases = [
            (
                "import sys;sys.stdout.buffer.write(b' '*257+b'\\n')",
                "header is too large",
            ),
            (
                "import sys;sys.stdout.buffer.write(b'{\"x\":0,\"y\":0,\"width\":1,\"height\":1}\\n'+b'x'*5)",
                "oversized sprite",
            ),
            (
                "import sys;sys.stdout.buffer.write(b'{\"x\":0,\"y\":0,\"width\":2,\"height\":2}\\n'+b'x'*3)",
                "incomplete sprite",
            ),
        ];
        for (worker, expected) in cases {
            let directory = tempfile::tempdir().unwrap();
            let package = directory.path().join("katman");
            std::fs::create_dir(&package).unwrap();
            std::fs::write(package.join("__init__.py"), "").unwrap();
            std::fs::write(package.join("caption_raster.py"), worker).unwrap();
            let result = rasterize_image(
                directory.path(),
                &Key::Caption(key.clone()),
                &state_for(&key),
            );
            assert!(
                matches!(result,Err(ref reason) if reason.contains(expected)),
                "expected {expected}"
            );
        }
    }

    #[test]
    fn caption_request_bounds_and_total_working_set_are_bounded_before_spawn() {
        let valid = key("Bounded caption");
        assert!(valid.bytes().is_some());
        let mut invalid = valid.clone();
        invalid.raster = [u32::MAX, 240];
        assert!(invalid.bytes().is_none());
        invalid = valid.clone();
        invalid.canvas = [u32::MAX, 240];
        assert!(invalid.bytes().is_none());
        invalid = valid.clone();
        invalid.captions = vec![json!({"text":"x"}); 129];
        assert!(invalid.bytes().is_none());
        invalid = valid.clone();
        invalid.captions = vec![json!({"text":"x".repeat(65537)})];
        assert!(invalid.bytes().is_none());
        invalid = valid.clone();
        invalid.captions = vec![json!({"text":"a\0b"})];
        assert!(invalid.bytes().is_none());
        invalid = valid.clone();
        invalid.style = Value::Null;
        assert!(invalid.bytes().is_none());

        let mut full = valid;
        full.canvas = [3840, 2160];
        full.raster = [3840, 2160];
        let mut second = full.clone();
        second.captions[0]["text"] = json!("Second");
        let mut third = full.clone();
        third.captions[0]["text"] = json!("Third");
        let mut cache = idle_cache();
        cache.request_layers(&[], &[full.clone(), second.clone(), third.clone()], &[]);
        assert!(!cache.caption_failed(&full));
        assert!(!cache.caption_failed(&second));
        assert!(cache.caption_failed(&third));
        assert_eq!(cache.shared.0.lock().unwrap().queue.len(), 2);
        assert!(
            cache
                .shared
                .0
                .lock()
                .unwrap()
                .wanted
                .iter()
                .map(|key| key.bytes().unwrap())
                .sum::<usize>()
                <= MAX_BYTES
        );
    }

    #[test]
    fn empty_caption_is_a_safe_transparent_one_pixel_sprite() {
        let mut key = key("");
        key.captions.clear();
        let raster = rasterize_image(&root(), &Key::Caption(key.clone()), &state_for(&key))
            .expect("empty caption sprite");
        assert_eq!(raster.bounds, [0, 0, 1, 1]);
        assert_eq!(raster.rgba, vec![0; 4]);
    }
}
