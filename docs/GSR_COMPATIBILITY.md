# GSR compatibility and remaining gaps

This describes the Layer 0.8 source against the pinned **GSR 6.1.7** fork, upstream commit `1d4946a16139b446e6308f741e9ba99f2ed5b3a7`. It distinguishes implemented integration from physical validation. See [provenance](../UPSTREAM.md), [desktop providers](desktop-providers.md), [portability audit](PORTABILITY_AUDIT.md) and [testing](TESTING.md).

## Capture sources and platforms

| GSR feature | Layer integration | Boundary |
| --- | --- | --- |
| NVIDIA, AMD and Intel; X11 and Wayland | GSR owns capture and advertised-codec discovery. CPU capture fallback is disabled. | Hardware/driver restrictions are inherited from GSR. A compiled encoder is not proof that a particular GPU supports it. |
| Direct monitors / KMS | An independent GSR video producer per selected monitor. | Requires matching KMS helpers and host permissions. Flatpak direct capture uses the system-install host-helper paths. |
| X11 monitors | GSR source inventory, optional XRandR geometry. | Actual hardware capture coverage is narrower than the implemented X11 path. |
| Portal | Explicit `capture_targets: ["portal"]`; GSR handles consent and session restoration. | No invisible fallback or consent bypass. Multiple portal selections are not represented as independent Layer monitor tracks. Saved media supplies final dimensions. |
| X11 window ID / focused window | Numeric IDs and `focused` pass through as explicit targets. | No window picker. Upstream focused-capture sizing restrictions apply. Not physically validated in this release cycle. |
| Camera / V4L2 | Explicit `/dev/video…` targets pass through to GSR. | No camera picker or camera-format UI; upstream device constraints apply. Not physically validated. |
| Native multi-source composition | Layer preserves independent producers and composes afterward. | Flattened native GSR composition is not a Layer capture mode. |
| Monitor layout | Optional Hyprland, Sway and X11/XRandR adapters enrich GSR-matching outputs. | Other sessions use an explicit synthetic horizontal layout. Layout enrichment never determines whether GSR can capture an output. |
| Virtual outputs | Available only through a GSR-supported direct inventory or explicit portal source. | No wf-recorder fallback. |
| Display hotplug | The Rust session polls GSR inventory every two seconds, adding/retiring display generations and reacting to geometry/mode changes. | Past segments remain available; probe failures do not retire sources. Explicit portal/window/camera selections stay static and never trigger periodic permission prompts. |
| Arbitrary capture GPU selection | GSR selects the capture/display GPU; profiles validate it. | Cross-GPU capture/encode selection is not promised. Export-device selection is a separate operation. |

The upstream [manual](../upstream/gpu-screen-recorder/gpu-screen-recorder.1) remains authoritative for source and driver restrictions. Advanced capture targets are configured in Layer's JSON settings; the normal GSR source/region selection does not restrict Layer's independent-display capture.

## Codec, color and timing

| Feature | Layer status |
| --- | --- |
| H.264, HEVC and AV1 | Exposed; explicit codecs must appear in GSR's advertised list. `auto` passes through to GSR. |
| HEVC/AV1 10-bit and HDR | Exact GSR codec names are retained in shared preferences and passed through when advertised. This does **not** establish HDR-aware editor preview/export or color-management parity. GSR source/session restrictions still apply. |
| VP8 / VP9 | Not exposed in Layer settings. |
| Quality presets / CBR | Shared GSR `medium`, `high`, `very_high`, `ultra` use QP; `custom` uses the configured CBR bitrate. Capture keyframe interval is one second for segmented replay. |
| Experimental Vulkan encoding, tune, yuv444, frame mode and color range | Not independently exposed; Layer is not a complete wrapper for every GSR CLI switch. |
| Independent audio | GSR Opus at 256 kbps in segmented Matroska. Metadata records the probed codec. |
| AAC / MP3 / lossless FLAC capture | Not selectable Layer capture formats. Pinned GSR disables FLAC. Editor audio export is separate. |

The fork adds exact `app:layer-node:<PipeWire-id>` selectors, first-packet timestamp sidecars and segmented-Matroska codec compatibility. Its `--info` reports the Layer node-selector extension and whether the FFmpeg segment muxer was compiled. The independent-track engine requires these extensions; stock GSR is not a drop-in replacement. See [command planning](../katman/hardware.py), [platform bridge](../katman/native_platform.py), [audio selectors](../upstream/gpu-screen-recorder/src/pipewire_audio.c) and [Rust capture](../native/recorder/src/capture.rs).

## Audio, replay and control

| Feature | Layer integration / boundary |
| --- | --- |
| PipeWire application roles | With GSR application-audio support and `pw-dump`, a persistent graph monitor discovers application/role groups and microphones. Voice and media remain separate only when the application exposes separate streams. Changed nodes start a new generation without erasing earlier media. |
| PulseAudio device fallback | Without application-node support, GSR enumerated output monitors and microphones become separate device tracks. Application/role separation is unavailable and a warning is recorded. Device inventory is static until capture restarts. |
| No available audio device | Display capture remains available with an explicit warning. |
| Mixed/default/inverse application selectors | Upstream capabilities, not exposed as Layer discovery policies. Concrete device names are preferred over duplicate default aliases. |
| RAM replay | Rust owns a bounded segmented tmpfs ring for independent tracks. No silent disk fallback. Upstream GSR native replay is not reused. |
| Ordinary recording plus replay | Encoded segments are shared; stopping one mode does not stop the other. Older ordinary-recording segments spool to disk. |
| Audio-only replay | GSR audio-only producers feed Layer's ring. This does not add upstream native `-r` support for audio-only capture. |
| Configurable saves | Buffer and short/long save durations follow the shared GSR preferences. Defaults are 300/60/300 seconds; CLI names `save-1`/`save-5` remain compatibility names. |
| Original Alt+Z and GSR shortcuts | The patched GSR UI retains normal keys and mirrors record/replay/save actions to Layer. Its normal shortcut settings also expose opening Layer. |
| Pause / native GSR socket | GSR pause/resume is mirrored into the ordinary Layer recording timeline. Replay continues independently. Repeated pause callbacks are idempotent. | Layer originals remain non-destructive, including paused intervals; the recording timeline and exports exclude those intervals. This is not a privacy/purge action. GSR itself owns native pause and its output file. |
| Streaming / WHIP | The Capture menu and `layer gsr` dispatch the normal GSR streaming commands using its existing configuration. | GSR owns the broadcast; the Layer edited composition is not a streaming mixer. No stream key is copied into Layer settings. |
| Region capture | Capture menu forwards GSR region recording and region screenshot actions. | GSR owns the selection and cropped output. Independent Layer sources stay full-screen for later editing/cropping. |
| Screenshots | Full-screen, region and window screenshot actions use GSR. | Images are saved to the normal GSR screenshot destination, not as editable Layer recording projects. Edited-frame PNG remains a separate editor action. |

## Desktop boundary and validation

Capture, RAM retention, snapshots and control do not depend on Hyprland. Optional adapters provide application identity and geometry on Hyprland, Sway and EWMH/X11. Native GNOME/KWin Wayland use `Desktop` naming and synthetic placement rather than private title scraping or shell extensions. The GSR overlay owns its own cross-desktop shortcut machinery; Layer's additional binding watcher is Hyprland-only. Other desktops can bind the CLI through their normal shortcut tools. Layer does not yet implement a separate GlobalShortcuts portal client.

Physical development checks exercised NVIDIA HEVC video and three independent Opus audio tracks, ordinary recording and RAM replay with the mandatory Hyprland API absent. Debian 13 Xfce/X11 and Weston, and openSUSE Leap 16 Xfce/X11 and nested KWin Wayland, exercised packaged UI startup. These VM checks used software rendering and do not prove KMS, portal consent, hardware encoding or all editor operations. The packaged full FFmpeg CLI also completed synthetic HEVC exports on physical NVIDIA NVENC and AMD VA-API devices. This is an export test, not desktop capture. Physical AMD/Intel capture, Intel export, GNOME/Mutter, portal, camera and focused-window validation remain open. No claim of every distribution or desktop being certified is made.
