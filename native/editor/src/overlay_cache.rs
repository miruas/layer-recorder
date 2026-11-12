// SPDX-License-Identifier: GPL-3.0-only
//! Bounded independent title sprites. Moving, tinting and fading a title never
//! invokes the rasterizer; only its font, text, size or clipping bounds do.
use eframe::egui;
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

const MAX_BYTES: usize = 32 * 1024 * 1024;
const MAX_KEYS: usize = 64;
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

#[derive(Default)]
struct Shared {
    wanted: HashSet<TextKey>,
    pending: HashSet<TextKey>,
    queue: VecDeque<TextKey>,
    stop: bool,
}
type State = Arc<(Mutex<Shared>, Condvar)>;
struct Pixels {
    key: TextKey,
    rgba: Result<Vec<u8>, String>,
}
struct Texture {
    handle: egui::TextureHandle,
    used: u64,
    bytes: usize,
}
pub struct Cache {
    shared: State,
    rx: mpsc::Receiver<Pixels>,
    textures: HashMap<TextKey, Texture>,
    failures: HashSet<TextKey>,
    clock: u64,
}
impl Cache {
    pub fn new(root: PathBuf) -> Self {
        let shared = Arc::new((Mutex::new(Shared::default()), Condvar::new()));
        let (tx, rx) = mpsc::sync_channel(2);
        let state = shared.clone();
        thread::spawn(move || {
            loop {
                let key = {
                    let (lock, condition) = &*state;
                    let mut shared = lock.lock().unwrap();
                    while shared.queue.is_empty() && !shared.stop {
                        shared = condition.wait(shared).unwrap();
                    }
                    if shared.stop {
                        return;
                    }
                    shared.queue.pop_front().unwrap()
                };
                let rgba = rasterize(&root, &key, &state);
                // Keep pending reserved until poll, including time in the channel.
                if tx.send(Pixels { key, rgba }).is_err() {
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
        }
    }
    pub fn request(&mut self, keys: &[TextKey]) {
        self.clock = self.clock.saturating_add(1);
        let mut wanted = HashSet::new();
        let mut bytes = 0usize;
        for (index, key) in keys.iter().enumerate() {
            if wanted.contains(key) {
                continue;
            }
            let size = key.bytes();
            if index >= MAX_KEYS
                || size.is_none()
                || bytes.saturating_add(size.unwrap_or(0)) > MAX_BYTES
            {
                if self.failures.len() < MAX_KEYS * 2 {
                    self.failures.insert(key.clone());
                }
                continue;
            }
            bytes += size.unwrap();
            wanted.insert(key.clone());
            if let Some(texture) = self.textures.get_mut(key) {
                texture.used = self.clock;
            }
        }
        let mut state = self.shared.0.lock().unwrap();
        let discarded: Vec<TextKey> = state
            .queue
            .iter()
            .filter(|key| !wanted.contains(*key))
            .cloned()
            .collect();
        state.queue.retain(|key| wanted.contains(key));
        for key in discarded {
            state.pending.remove(&key);
        }
        state.wanted = wanted;
        for key in keys {
            if state.wanted.contains(key)
                && !self.textures.contains_key(key)
                && !self.failures.contains(key)
                && state.pending.insert(key.clone())
            {
                state.queue.push_back(key.clone());
            }
        }
        self.shared.1.notify_one();
        // Failures only need to survive while relevant to the current scene.
        if self.failures.len() > MAX_KEYS * 2 {
            self.failures.retain(|key| keys.contains(key));
        }
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
                    if pixels.key.bytes() != Some(rgba.len()) {
                        self.failures.insert(pixels.key);
                        continue;
                    }
                    let wanted = self.shared.0.lock().unwrap().wanted.clone();
                    while self
                        .textures
                        .values()
                        .map(|texture| texture.bytes)
                        .sum::<usize>()
                        + rgba.len()
                        > MAX_BYTES
                    {
                        let old = self
                            .textures
                            .iter()
                            .filter(|(key, _)| !wanted.contains(*key))
                            .min_by_key(|(_, texture)| texture.used)
                            .map(|(key, _)| key.clone());
                        if let Some(old) = old {
                            self.textures.remove(&old);
                        } else {
                            break;
                        }
                    }
                    if self
                        .textures
                        .values()
                        .map(|texture| texture.bytes)
                        .sum::<usize>()
                        + rgba.len()
                        > MAX_BYTES
                    {
                        self.failures.insert(pixels.key);
                        continue;
                    }
                    let image = egui::ColorImage::from_rgba_unmultiplied(
                        [pixels.key.width as usize, pixels.key.height as usize],
                        &rgba,
                    );
                    let handle =
                        ctx.load_texture("Title sprite", image, egui::TextureOptions::LINEAR);
                    self.textures.insert(
                        pixels.key,
                        Texture {
                            handle,
                            used: self.clock,
                            bytes: rgba.len(),
                        },
                    );
                }
                Err(reason) if reason == SUPERSEDED => {
                    let mut state = self.shared.0.lock().unwrap();
                    if state.pending.insert(pixels.key.clone()) {
                        state.queue.push_back(pixels.key);
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
        self.textures.get(key).map(|texture| texture.handle.id())
    }
    pub fn ready(&self, key: &TextKey) -> bool {
        self.textures.contains_key(key)
    }
    /// Call after request: rejected working-set entries are explicit failures.
    pub fn failed(&self, key: &TextKey) -> bool {
        self.failures.contains(key)
            || key.bytes().is_none()
            || !self.shared.0.lock().unwrap().wanted.contains(key)
    }
    pub fn clear(&mut self) {
        self.textures.clear();
        self.failures.clear();
        let mut state = self.shared.0.lock().unwrap();
        state.wanted.clear();
        let discarded: Vec<TextKey> = state.queue.drain(..).collect();
        for key in discarded {
            state.pending.remove(&key);
        }
    }
}
impl Drop for Cache {
    fn drop(&mut self) {
        let mut state = self.shared.0.lock().unwrap();
        state.stop = true;
        state.wanted.clear();
        state.queue.clear();
        self.shared.1.notify_one();
    }
}

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        // Kill the Python worker and any FFmpeg/Fontconfig descendant together.
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.wait();
    }
}
fn cancelled(state: &State, key: &TextKey) -> bool {
    let state = state.0.lock().unwrap();
    state.stop || !state.wanted.contains(key)
}
fn nonblocking(fd: i32) -> bool {
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        flags >= 0 && libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) >= 0
    }
}
fn rasterize(root: &Path, key: &TextKey, state: &State) -> Result<Vec<u8>, String> {
    let bytes = key
        .bytes()
        .ok_or("Invalid title sprite dimensions or text")?;
    if cancelled(state, key) {
        return Err(SUPERSEDED.into());
    }
    let child = Command::new("python3")
        .args(["-m", "katman.overlay_raster"])
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map_err(|error| error.to_string())?;
    let mut process = Process(child);
    let mut input = process.0.stdin.take();
    let mut output = process.0.stdout.take().ok_or("Missing sprite output")?;
    if !nonblocking(output.as_raw_fd())
        || !input
            .as_ref()
            .is_some_and(|input| nonblocking(input.as_raw_fd()))
    {
        return Err("Could not configure cancellable title rasterization".into());
    }
    let request = key.request();
    let mut written = 0usize;
    let mut read = 0usize;
    let mut rgba = vec![0; bytes];
    let started = Instant::now();
    loop {
        if cancelled(state, key) {
            return Err(SUPERSEDED.into());
        }
        if started.elapsed() > Duration::from_secs(10) {
            return Err("Title rasterization timed out".into());
        }
        if let Some(stdin) = input.as_mut() {
            match stdin.write(&request[written..]) {
                Ok(0) => return Err("Text rasterizer closed its input".into()),
                Ok(count) => {
                    written += count;
                    if written == request.len() {
                        input = None;
                    }
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(error) => return Err(error.to_string()),
            }
        }
        match output.read(&mut rgba[read..]) {
            Ok(0) => return Err("Text rasterizer returned an incomplete sprite".into()),
            Ok(count) => {
                read += count;
                if read == bytes {
                    return Ok(rgba);
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                ) => {}
            Err(error) => return Err(error.to_string()),
        }
        thread::sleep(Duration::from_millis(2));
    }
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
        state.wanted.insert(key.clone());
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
        state.0.lock().unwrap().wanted.insert(key.clone());
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
