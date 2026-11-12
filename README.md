# Layer — GPU Screen Recorder fork

A **GPL-3.0-only source fork of GPU Screen Recorder**, with a native independent-track editor. The original recorder is developed by dec05eba; its source, copyright notices and Git history are preserved here. See [upstream provenance and fork changes](UPSTREAM.md).

Layer is a native Linux recorder and independent-track video editor. Capture displays and application audio separately, keep the last minute or five minutes in RAM, then decide which screens and sounds belong in the final video.

Layer supports NVIDIA NVENC and AMD/Intel VA-API paths. The library and editor share one native Rust window; no browser or Qt runtime is required. A **Rust GSR-only core** supervises capture. GSR discovers sources; Hyprland, Sway and X11 metadata are optional adapters. Other desktop/session paths have explicit verification limits in the [compatibility matrix](docs/GSR_COMPATIBILITY.md).

[GSR features, Layer gaps and Hyprland separation](docs/GSR_COMPATIBILITY.md)

## GPU support

| GPU | Display capture | Virtual / permission-based capture | Export | Verification |
| --- | --- | --- | --- | --- |
| NVIDIA | GPU Screen Recorder, native GPU encoder | GSR portal, when available | H.264, HEVC, AV1 through NVENC | Real capture and hardware exports tested |
| AMD | GPU Screen Recorder, native GPU encoder | GSR portal, when available | H.264, HEVC, AV1 when supported by the GPU/driver | Real VA-API capture and H.264/HEVC exports tested |
| Intel | GPU Screen Recorder, native GPU encoder | GSR portal, when available | H.264, HEVC, AV1 when supported by the GPU/driver | Command/configuration paths tested; physical Intel verification remains open |

Codec availability depends on the GPU generation, driver and FFmpeg build. A compiled encoder is not proof that the installed GPU supports it. Layer reports unsupported selections rather than silently changing capture to CPU encoding. CPU H.264, HEVC and AV1 exports are also available.

See [GPU support and driver setup](docs/GPU_SUPPORT.md) for profiles, limitations and hardware checks.

## Features

- Independent display, microphone and application-audio capture, including sources that appear after recording starts.
- GSR-owned capture controls: live broadcast, region recording and full-screen/region/window screenshots use the normal GSR settings and output destinations. The overlay has a single Recordings entry for the native editor.
- Dynamic display hotplug keeps previous source generations and reconciles reconnects or resolution changes without resetting the RAM ring.
- Ordinary-recording pause/resume is mirrored into the editable timeline while replay remains continuous; source media stays non-destructive.
- Configurable **RAM replay ring**, with separate short/long save presets (60 and 300 seconds by default). Normal recording and replay can run together.
- Time-window source filtering: retired or genuinely silent application streams are omitted from new saves; microphone tracks remain available.
- Readable audio roles such as **Discord — Voice chat**, **Discord — Media** and **Notifications**, when exposed separately by PipeWire.
- Recording library and native Studio with independent video/audio tracks, move/trim/split, snapping, zoom, multi-selection, copy/paste, undo/redo and source-preserving append/overlay imports.
- Display composition, source/canvas crop, transforms and keyframes, layout presets, opacity, borders, color controls, LUTs and stabilization.
- Timed titles, editable drawings, blackout/blur/pixelation and optical-flow privacy tracking.
- SRT import/export and optional **offline** automatic subtitles with whisper.cpp.
- Independent audio gain/keyframes, fades, denoise, normalization and microphone voiceover.
- Variable speed segments, reverse/freeze, proxies, rendered audiovisual preview and frame stepping.
- Configurable video, audio-only, batch and PNG exports. Original recording files are preserved.
- Optional GSR Alt+Z integration with mirrored record/replay shortcuts. Normal save successes remain silent; errors stay visible.

## Download and install

For the portable 0.8 packaging path, use the [Flatpak build and installation guide](docs/FLATPAK.md). It bundles one Freedesktop runtime for Debian, Ubuntu, Fedora, openSUSE, Arch and other Flatpak-capable Linux distributions, rather than sharing distribution-specific Python/FFmpeg ABIs. Physical capture still follows GSR's driver and permission requirements. See the [portability audit](docs/PORTABILITY_AUDIT.md) for tested and pending coverage.

The 0.8 portable preview is published separately from the latest stable 0.7 Arch/CachyOS package. See [all releases](https://github.com/miruas/layer-recorder/releases) and the validation limits before choosing a preview.


[**Download the ready-to-install release**](https://github.com/miruas/layer-recorder/releases/latest) for **Arch / CachyOS x86_64**. One package supports NVIDIA, AMD and Intel, subject to GPU/driver capabilities. It includes compiled capture helpers, the patched GSR overlay and offline subtitle engine. No compilation is required for the package; install the required vendor drivers and system GSR KMS broker first.

```sh
sudo pacman -U ./layer-recorder-0.7.0-1-x86_64.pkg.tar.zst
layer ui
```

[Package installation and checksum verification](docs/INSTALLATION.md) · [Build from source](docs/BUILDING.md) · [Release history](https://github.com/miruas/layer-recorder/releases)

The **Recording settings** button opens the bundled normal GSR replay settings. GSR owns codec, capture quality, frame rate, replay durations and shared shortcuts. The patched GSR global settings also provide **Open Layer editor**, default Meta+Alt+Z. Unrelated GSR preferences are preserved.

## Source quick start

For a distribution-independent build, follow the Flatpak guide. Native source builds need Python 3.12+, stable Rust, FFmpeg and GSR development libraries. PipeWire enables independent application roles; PulseAudio device capture is available without it. Capture helpers target current FFmpeg APIs; see [installation](docs/INSTALLATION.md) for exact dependencies and other distributions.

```sh
git clone https://github.com/miruas/layer-recorder.git
cd layer-recorder
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . meson ninja
python tools/build-native.py gpu-screen-recorder
python tools/build-native.py recorder
python tools/build-native.py editor
python install.py --profile auto --enable
```

Add `~/.local/bin` to your session's PATH. Keep the checkout and virtual environment at their installed locations.

```sh
layer doctor
layer ui
layer replay
layer save-1
layer save-5
```

Saving before the ring fills includes the footage available since capture started. Saved clips go to `~/Videos/Layer` by default. No personal settings, recordings or speech models are distributed. Compiled installable packages are published as release assets; Git tracks source and documentation. The original public upstream color-space documentation figure is retained with a verified provenance hash.

For AMD or Intel, choose `--profile amd` or `--profile intel` on a fresh install, or edit the existing configuration. Existing settings are never overwritten by reinstalling.

## Configuration

Preferences live in `${XDG_CONFIG_HOME:-~/.config}/katman/settings.json`. The compatible internal name and `.katman` project extension are retained.

- [Automatic profile](examples/config.auto.json)
- [NVIDIA profile](examples/config.nvidia.json)
- [AMD profile](examples/config.amd.json)
- [Intel profile](examples/config.intel.json)
- [JSON schema](examples/settings.schema.json)
- [Every setting explained](docs/CONFIGURATION.md)

```sh
layer config show
layer config validate
# Create a fresh configuration only; an existing file is never replaced.
layer config init --profile amd
```

`LAYER_CONFIG` selects an alternate settings file. Use normal GSR settings for common capture preferences; `LAYER_GSR_CONFIG` selects its shared configuration file and `gsr_preferences: false` opts out. Changes apply when the capture engine next starts; save active captures before restarting it.

## Recording, editing and shortcuts

Enable replay, record normally, or use the optional desktop bindings. Select a saved recording in the library to edit it in the same window. Toggle tracks to exclude a screen or sound, then export an ordinary video file.

[Recording and editor guide](docs/USAGE.md) · [Optional GSR menu and hotkey integration](docs/INTEGRATION.md) · [Suggested Hyprland bindings](examples/hyprland.conf)

To start replay automatically at graphical login, enable the service **and** set `auto_replay` to `true`. The optional GSR integration can instead mirror GSR's replay autostart. Installing Layer does not rewrite your compositor configuration or replace packaged GSR binaries.

## Privacy and storage

The rolling capture lives in a private memory-backed directory under `/dev/shm/layer-<uid>/`. Saved projects are written to the selected recording folder. During long normal recordings, older closed segments spill to disk so the RAM ring stays bounded. Preview/proxy caches remain disk caches.

Window titles are not used for application folder names. Speech recognition runs locally when its optional engine/model is installed. Independent audio separation follows the streams supplied by PipeWire; a single mixed upstream stream cannot be reconstructed into separate voice/media sources.

[Storage, project format and architecture](docs/ARCHITECTURE.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

## Development and validation

```sh
python -m unittest discover -s tests -v
xvfb-run -a python tools/smoke-ui.py
python tools/check-public-tree.py
```

Hardware tests are opt-in. CI exercises Python versions, portable media tests, GPU profile validation, native settings and package builds. [Testing details and verification boundaries](docs/TESTING.md) distinguish real hardware checks from command-planning tests.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) · [Licenses and upstream components](NOTICE.md)
