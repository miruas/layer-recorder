# Capture architecture

Layer 0.7 moves capture supervision, JSON IPC, RAM allocation, incremental segment indexing, replay retention, ordinary-recording spooling and independent-source snapshots into `native/recorder` (Rust). `capture.py`, `daemon.py` and `buffer_storage.py` are compatibility adapters; there is no Python capture backend or automatic wf-recorder fallback.

GSR produces every video and audio track. FFmpeg remains the lossless container-copy/probe tool when saving, and the existing Python editor/export pipeline remains intact. The metadata/configuration bridge is one persistent Python process; it never receives media. The PipeWire graph is one persistent `pw-dump --monitor` stream rather than repeated complete subprocess dumps four times a second.

Status is cached separately from capture commands, so a replay save or encoder flush does not block status queries. The control socket retains newline JSON request/response compatibility, private permissions, singleton locking and existing action names. The queue is bounded; shutdown preserves ordinary recordings.

Each selected display has its own GSR encoder process. Each discovered application/role or microphone group has a GSR audio-only producer. Node selectors preserve voice/media separation; changing nodes retires one generation and starts another without erasing prior media. Audio-only GSR cannot use native replay, so Rust owns a common segmented timeline and bounded tmpfs ring. Ordinary recordings move older **closed** segments to a durable cache spool; replay never silently falls back to disk. First-frame timestamps align streams, and saved `.katman` projects retain version-1 independent media/timing compatibility.

GSR is the authoritative source inventory. Optional compositor adapters enrich logical geometry, application names and shortcuts. Without Hyprland, placement is explicitly synthetic horizontal layout and application naming falls back to Desktop. See [compatibility](GSR_COMPATIBILITY.md) for upstream features and remaining gaps.

## Existing editor and media format reference

## Data flow

```text
GSR source inventory ── GSR video producers ── hardware-encoded segments ─┐
PipeWire events ── GSR independent audio producers ── Opus segments ─────────────────┤
                                                                         ↓
                                                         private RAM ring (300s)
                                                                         ↓ save
                                                        project.katman + media/
                                                                         ↓ edit
                                                   independent Studio tracks/clips
                                                                         ↓
                                                shared FFmpeg preview/export graph
```

Application playback routing is preserved. Layer creates capture links to application outputs; it does not move them away from speakers/headphones. Recycled ports reconnect; a role change removes only the old owned capture link. Microphones use their appropriate source links.

GPU encoding happens before video enters the RAM ring. Each display and audio role has separate media/timing metadata. Closed segment selection protects replay endpoints; capture warnings record unavailable tails instead of claiming full coverage.

## RAM versus disk

Replay media and capture segment metadata live under `/dev/shm/layer-<uid>/buffer-*`, in a directory owned by the user with mode0700. The filesystem must be tmpfs/ramfs. Layer does not silently switch replay to SSD.

Older unsaved replay segments expire. During ordinary recording, closed segments outside the replay ring are copied into a durable spool under the application cache. Saved ordinary recordings can therefore combine disk-spooled history and the current RAM ring without unbounded ring growth.

Saved projects use the configured recording directory. Thumbnails, waveforms, proxies, stabilization analysis and rendered previews use the application cache. Uninstall preserves projects/settings/caches. Normal shutdown removes the active capture ring; capture/save failures may retain diagnostic/recoverable pieces.

## Project format

```text
recording/
  project.katman
  media/
    independently captured or imported sources
```

The JSON project uses format `katman`, version1. Sources include relative media paths, type/name, timeline start/trim and media duration. `studio` stores independent tracks/clips, transforms, animation, audio processing, timed annotations and subtitles. Original media is not overwritten by editing.

Paths loaded from a project must remain inside its project folder. Treat imported media/projects as untrusted data and keep FFmpeg and Qt updated. Optional LUT/stabilization paths refer to local analysis/assets; moving a project does not create a media-relinking workflow.

## Modules

| Module | Responsibility |
| --- | --- |
| `configuration.py` | Defaults, profile generation, strict settings validation |
| `hardware.py` | GPU discovery, GSR capability parsing, vendor-neutral command planning |
| `buffer_storage.py` | Private memory filesystem allocation |
| `native/recorder/src` | Rust capture lifecycle, discovery, RAM ring/spool, snapshots and IPC |
| `capture.py` / `daemon.py` / `buffer_storage.py` | Python compatibility clients / native executable entry points |
| `audio_identity.py` / `identity.py` | Stream roles and application-only naming |
| `studio_model.py` | Independent edits, timing and animation |
| `studio_render.py` | One FFmpeg compositor for frames, playback and export |
| `studio_jobs.py` | Proxies, stabilization, tracking and local subtitles |
| `studio*.py`, `ui.py`, `theme.py` | Native editor/library, jobs and semantic theme roles |
| `bridge.py` | Detached, idempotent GSR action mirroring |

## Compatibility boundaries

Capture is GSR/PipeWire based, with optional Hyprland integration. KDE/GNOME/X11 session testing remains open. GPU Screen Recorder chooses its capture device. Source separation cannot unmix a single upstream audio stream. Motion tracking and speech recognition require review. Project versioning, autosave recovery UI and media relinking are not new features of this release.
