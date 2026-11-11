# Capture and editor architecture

Layer 0.8 combines a Rust recording service and a single native Rust archive/editor. The pinned Layer GSR fork produces every captured video/audio track. Python remains a headless configuration bridge and media worker; there is no Python GUI, automatic wf-recorder fallback or separate second editor window.

## Data flow

```text
GSR source inventory -> independent GSR video producers ----+
PipeWire graph or GSR device inventory -> GSR audio --------+-> Rust RAM ring
                                                               | save
                                                      project.katman + media/
                                                               | open
                                                    Rust archive/editor window
                                                               | background jobs
                                               Python model/render/jobs -> FFmpeg
```

The metadata/configuration bridge never receives media. With application-audio support, one persistent `pw-dump --monitor` stream supplies dynamic application/role discovery. Without it, a startup GSR audio-device inventory provides separate output-monitor and microphone tracks, with an explicit loss of application separation. With no usable audio devices, display capture can continue with a warning.

## Capture service

`native/recorder` owns capture supervision, JSON IPC, RAM allocation, incremental segment indexing, replay retention, ordinary-recording spooling and independent-source snapshots. `capture.py`, `daemon.py` and `buffer_storage.py` are compatibility entry points/clients. Status is cached separately from capture commands, so encoder flushes and saves do not hold the status-query path.

Each display has its own hardware-encoded producer. PipeWire application/role generations and microphones have separate GSR audio-only producers. Exact node selectors preserve voice/media separation when the upstream application exposes distinct streams. A changed node retires one generation and starts another; earlier captured media remains available. Playback routes are not moved away from speakers/headphones.

GSR audio-only capture does not use native replay. Rust retains all producers on a common segmented timeline. First-packet sidecars align streams; snapshot metadata records timing, presence and coverage warnings. Saves omit out-of-window and genuinely silent application sources while preserving independent editable media.

GSR is authoritative for capture inventory. Optional Hyprland, Sway and X11 adapters enrich geometry and application naming. Missing metadata providers do not block capture. Native GNOME/KWin Wayland use `Desktop` and synthetic placement. See [Desktop providers](desktop-providers.md).

## RAM and disk

Replay media and segment metadata live in private mode-0700 directories under `/dev/shm/layer-<uid>/buffer-*`. The filesystem must be tmpfs/ramfs and have enough free memory. Replay never silently switches to disk.

Expired unsaved replay segments are removed. During ordinary recording, older closed segments move to a durable application-cache spool; a save can combine spooled history and the current RAM ring. Saved projects use the configured library. Thumbnails, waveforms, proxies, stabilization data and rendered previews use caches. Normal shutdown removes the active ring; failures may retain recoverable diagnostics/media. Uninstall preserves user projects/settings/caches.

## Native archive/editor

`native/editor` runs one eframe window for the library, composition, timeline and inspector. Project reads, fonts, settings resolution, media analysis, preview/export and save work are dispatched away from the UI thread. Workers return tagged results so stale document jobs cannot overwrite a newly opened edit. Subprocess cancellation and playback teardown do not synchronously wait on the GUI thread.

Autosave coalesces edits for up to 100 ms on a dedicated writer. It preserves current unknown metadata and appends newly imported source IDs. Media jobs and document reopening wait for asynchronous persistence barriers; cancelling a waiting job never starts its Python process. Closing waits for a flush acknowledgement while the interface stays responsive. Failed snapshots remain available for retry; closing without saving is an explicit user choice.

The Rust model preserves version-1 JSON fields and original sources. It supports independent clips, undo/redo, splitting/trimming, speed/reverse/freeze, keyframes, transforms, annotations and subtitles. The Python media worker uses the same compositor for paused frames, audiovisual preview and export. Audio waveform analysis is cached; previews may use proxies while export uses originals. Voiceover is a temporary GSR audio-only recording imported as a separate source. Batch export processes saved projects without overwriting their media.

The public interface uses a neutral palette. Personal theme integrations are not part of this source tree.

## Project format

```text
recording/
  project.katman
  media/
    independent captured or imported sources
```

JSON format `katman`, version 1, stores sources and their relative paths, timing and media duration. The `studio` model stores tracks/clips, transforms, animation, audio processing, timed annotations and subtitles. Editing saves decisions and never overwrites original media. Keep the project with its media directory.

Loaded source paths must remain within the project directory. Keep media decoders updated when opening imported files. Optional LUT and stabilization paths refer to local assets/analysis; the application does not add a media-relinking or project-management system.

## Modules

| Module | Responsibility |
| --- | --- |
| `native/recorder/src` | Capture lifecycle, discovery, RAM ring/spool, snapshots and IPC. |
| `native/editor/src` | Native archive/editor, Rust document model, jobs, IPC, playback and voiceover. |
| `configuration.py`, `gsr_preferences.py` | Layer validation/profiles and read-only resolution of shared GSR capture preferences. |
| `hardware.py`, `audio_plan.py`, `native_platform.py` | Capability validation and GSR capture/audio plans. |
| `platform_capture.py`, `identity.py`, `audio_identity.py` | Optional desktop metadata and readable application/role identity. |
| `editor_worker.py` | Headless background media job protocol. |
| `studio_model.py`, `studio_render.py`, `studio_jobs.py` | Media-model validation, FFmpeg composition and analysis/import/subtitle jobs. |
| `ui.py` | Launcher for the native Rust application. |
| `bridge.py`, patched GSR UI | Detached idempotent action mirroring and shared normal-GSR control. |

## Settings and limits

Normal GSR `config_ui` is the default source for capture codec, quality, FPS, bitrate, replay buffer and short/long saves. The read-only resolver preserves exact codec variants and leaves library/export preferences separate. Existing captures retain their starting profile until restarted/reloaded. The overlay owns shared shortcut editing.

GSR chooses capture hardware; export-device choices are separate. A single combined upstream audio stream cannot be unmixed. HDR codec passthrough is not an HDR color-managed editor guarantee. See [compatibility and remaining gaps](GSR_COMPATIBILITY.md) and [portability evidence](PORTABILITY_AUDIT.md).
