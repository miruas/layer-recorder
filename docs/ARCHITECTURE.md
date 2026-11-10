# Architecture and storage

## Data flow

```text
Hyprland displays ── GSR / patched wf-recorder ── hardware-encoded segments ─┐
PipeWire streams ── passive capture links ── FLAC segments ─────────────────┤
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
| `capture.py` / `daemon.py` | Capture lifecycle, discovery, ring/spool, save requests |
| `audio_identity.py` / `identity.py` | Stream roles and application-only naming |
| `studio_model.py` | Independent edits, timing and animation |
| `studio_render.py` | One FFmpeg compositor for frames, playback and export |
| `studio_jobs.py` | Proxies, stabilization, tracking and local subtitles |
| `studio*.py`, `ui.py`, `theme.py` | Native editor/library, jobs and semantic theme roles |
| `bridge.py` | Detached, idempotent GSR action mirroring |

## Compatibility boundaries

Capture is integrated with Hyprland/PipeWire; KDE/GNOME/X11 capture is not advertised. GPU Screen Recorder chooses its capture device. Source separation cannot unmix a single upstream audio stream. Motion tracking and speech recognition require review. Project versioning, autosave recovery UI and media relinking are not new features of this release.
