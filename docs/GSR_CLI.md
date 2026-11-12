# GSR capture options in Layer

Layer uses the bundled `gpu-screen-recorder` for each independent video/audio
source. Configure `capture_advanced` as a JSON object in Layer settings. The
Advanced settings editor validates the same schema before saving. An empty
object preserves existing behavior. There is no raw command-line escape hatch:
arguments are passed directly to the process, never through a shell.

```json
{
  "capture_quality": "high",
  "capture_advanced": {
    "bitrate_mode": "vbr",
    "framerate_mode": "vfr",
    "pixel_format": "yuv420",
    "cursor": true,
    "keyint": 1,
    "audio_codec": "opus",
    "audio_bitrate": 256,
    "exclude_metadata": true
  }
}
```

## Structured advanced settings

Omitted fields retain Layer/GSR defaults. Video settings apply independently to
each video producer; audio settings apply to both initial device tracks and new
application streams discovered during capture.

| `capture_advanced` key | GSR flag | Accepted values / behavior |
|---|---|---|
| `bitrate_mode` | `-bm` | `auto`, `qp`, `vbr`, `cbr`. CBR requires `capture_quality: custom` and uses `bitrate`; the other modes require a named quality preset. Default: CBR for custom, QP for a preset. |
| `framerate_mode` | `-fm` | `cfr`, `vfr`, `content`. Omitted: GSR VFR. Content mode requires X11 or portal capture. |
| `pixel_format` | `-pixfmt` | `yuv420`, `yuv444`. GSR may warn and fall back to 420 if the codec/GPU lacks 444 support. |
| `color_range` | `-cr` | `limited`, `full`. Full range has player compatibility limits. |
| `tune` | `-tune` | `performance`, `quality`; currently NVIDIA only. |
| `keyint` | `-keyint` | Finite number from 0 to 2 seconds. Default 1. Layer caps the native 500-second maximum to preserve two-second segment boundaries and bounded save waits. |
| `encoder` | `-encoder` | `gpu` (default), `cpu`. CPU supports only `auto`/`h264`. This remains GSR capture. |
| `fallback_cpu_encoding` | `-fallback-cpu-encoding` | Boolean; default false. Explicit opt-in to upstream CPU fallback. |
| `cursor` | `-cursor` | Boolean; upstream default true. |
| `low_power` | `-low-power` | Boolean; AMD-specific upstream behavior. |
| `restore_portal_session` | `-restore-portal-session` | Boolean; default true for Layer portal sources. Only sent for a portal source. |
| `portal_session_token_filepath` | `-portal-session-token-filepath` | Nonempty path; `~` expanded. Only sent for portal. Do not end the path with `/`. |
| `region` | `-region` | Object with integer `width`, `height` (0..32768), `x`, `y` (-131072..131072). Used with a `region` capture target. Zero width/height asks GSR for the containing monitor. |
| `audio_codec` | `-ac` | `opus` (default), `aac`, `mp3`. FLAC is excluded: this pinned GSR silently substitutes Opus for FLAC. MP3 requires the matching FFmpeg encoder. |
| `audio_bitrate` | `-ab` | Integer kbps, 0..50000; default 256. Zero lets GSR choose. |
| `overclock` | `-oc` | Boolean. Deprecated upstream; GSR reports that it has no effect on modern GPUs. |
| `verbose` | `-v` | Boolean; default false. Writes diagnostics into each capture log. |
| `gl_debug` | `-gl-debug` | Boolean. |
| `exclude_metadata` | `-exclude-metadata` | Boolean; suppresses metadata GSR would add. Layer project source labels remain present. |
| `plugins` | repeated `-p` | At most 16 absolute `.so` paths. Loads native plugin code into every video producer; use plugins you intend to execute. Paths may contain spaces. |
| `video_options` | `-ffmpeg-video-opts` | Restricted codec option object, below. |
| `audio_options` | `-ffmpeg-audio-opts` | Restricted codec option object, below. |

Codec option values must be simple strings/numbers/booleans of at most 128
characters. `;`, `=`, whitespace, braces and control characters are rejected;
booleans become `true`/`false`. Keys are sorted before serialization. The exact
encoder decides whether a particular option/value is available; GSR's capture
log reports unsupported combinations. These are codec-private options, not
shell arguments or arbitrary FFmpeg options.

- `video_options`: `preset`, `profile`, `level`, `rc_mode`, `qp`, `cq`,
  `b_ref_mode`, `spatial_aq`, `temporal_aq`, `aq-strength`, `look_ahead`, `coder`.
- `audio_options`: `aac_coder`, `aac_pce`, `compression_level`, `application`,
  `frame_duration`, `vbr`, `cutoff`.

## Existing top-level settings

| GSR flag | Layer setting |
|---|---|
| `-w` | `capture_targets`: individual sources. Empty means discovered monitors. Composite `|` sources are excluded to retain editable independent sources. |
| `-f` | `fps` (1..240 in Layer). |
| `-s` | `capture_resolution`: native, preset or bounded even dimensions. |
| `-k` | `capture_codec`: `auto`, `h264`, `hevc`, `hevc_10bit`, `hevc_hdr`, `av1`, `av1_10bit`, `av1_hdr`, `vp8`, `vp9`, `h264_vulkan`, `hevc_vulkan`, `hevc_hdr_vulkan`, `hevc_10bit_vulkan`, `av1_vulkan`, `av1_hdr_vulkan`, `av1_10bit_vulkan`. Availability is checked against GSR capabilities for GPU encoding. |
| `-q` | `capture_quality` preset or `bitrate` with `custom`. |
| `-a` | Layer discovers separate application-role/microphone tracks, or separate PulseAudio devices. Arbitrary mixtures/inverse-app selectors are not exposed; they lose independent-source semantics. |

HDR is unavailable in upstream X11 and portal capture. Vulkan codecs are
experimental. Capture and GPU encoding normally use the display's GPU; a vendor
profile does not provide arbitrary cross-GPU selection. CPU fallback, plugins,
codec settings and HDR must be tested on the actual machine. Exposing an option
does not imply that every driver/source supports it.

## Managed or inapplicable recorder flags

Every recorder flag registered by the pinned `src/args_parser.c` is accounted
for above or below. Layer intentionally owns storage, replay, synchronization and
project creation instead of allowing flags that bypass those contracts.

| GSR flag | Policy / equivalent |
|---|---|
| `-c`, `-o` | Managed segment muxer and per-track RAM paths. Saved projects are chosen through Layer library settings. Streaming URLs and image outputs are not capture settings. |
| `-ffmpeg-opts` | Managed: two-second Matroska segments, segment list, reset timestamps and flush behavior. No arbitrary muxer overrides. |
| `-write-first-frame-ts` | Always yes; required for cross-source monotonic synchronization. |
| `-r`, `-replay-storage` | Layer's own bounded RAM ring and durable ordinary-recording spool. `buffer` controls duration. Native audio-only GSR replay is unsupported. |
| `-ro` | Layer ordinary recording can retain segments while replay continues; no second native replay recording output. |
| `-restart-replay-on-save` | Not exposed: replay snapshots retain their source ring; Layer save semantics are non-destructive. |
| `-df` | Layer library/application/timestamp project folders own organization. |
| `-sc` | Not exposed: GSR would call a hook for an internal producer lifecycle, not a completed Layer project. A project-level hook requires a separate lifecycle API. |
| `-auth` / `GSR_AUTH` | Streaming-only authentication; Layer's independent RAM capture does not stream. No streaming secrets are stored by this schema. |
| `-ipc` | GSR producer lifecycle is owned by Layer's daemon. Arbitrary native producer IPC could stop/pause only one track and break synchronization. |

Individual capture targets accept validated GSR source modifiers, for example
`DP-1;hflip=true;width=50%` or
`/dev/video0;pixfmt=mjpeg;camera_fps=60;camera_width=1920;camera_height=1080`.
Supported keys are `x`, `y`, `width`, `height` (integer pixels or integer `%`),
`halign`/`valign` (`start`, `center`, `end`), `hflip`/`vflip` (`true`, `false`),
`pixfmt` (`auto`, `yuyv`, `mjpeg`), and nonnegative integer `camera_fps`,
`camera_width`, `camera_height`. Duplicate/unknown modifiers are rejected.
Typed `monitor:`, `window:` and `v4l2:` prefixes resolve the underlying source.
Named monitor targets with modifiers retain inventory/hotplug matching by their
base name. Explicit screen/screen-direct/region/window/camera/portal targets are
static sources. Source modifiers change the recorded pixels; the original
source layout metadata is retained for Layer's initial editor placement.

Composite `|` sources, arbitrary audio mixing, screenshots, streaming, hooks and
unrestricted native output/muxer options are available through the separate
native mode: `layer gsr-native -- <gpu-screen-recorder arguments>`. That mode
runs the bundled fork with exact native arguments; it does not create Layer's
independent-source projects or enforce its RAM ring. Use native `-h` for help.
Do not point it at an active Layer output directory.

## Discovery commands and `gsr-cli`

`gpu-screen-recorder --info`, `--list-monitors`, `--list-capture-options` (optional
DRM card path), `--list-audio-devices`, `--list-application-audio`,
`--list-v4l2-devices`, `--version`, `-h` and `--help` are diagnostic/inventory
commands, not persistent capture settings. Layer uses the relevant inventory
commands and displays capability failures instead of silently changing codecs.

`gsr-cli` is a separate IPC controller, not a capture-options executable.
Its complete command set in the pinned `gsr-cli.1` is:

| Command | Native meaning / Layer handling |
|---|---|
| `status` | Probe a native GSR socket. Layer status is served by its own daemon. |
| `stop` | Stop/save native recording. Layer stops all producers and creates an independent-source project. |
| `toggle-pause`, `set-paused true\|false` | Native recording-only pause. Layer pauses its ordinary-recording timeline while replay continues. |
| `toggle-replay-recording`, `start-replay-recording`, `stop-replay-recording` | Native `-ro` control. Layer records through its own spool and timeline. |
| `save-replay [seconds] [restart-replay=true\|false]` | Native single-session replay save. Layer snapshots synchronized independent sources through its daemon; native audio-only replay is unavailable. |

For an explicitly separate native GSR instance, `layer gsr-ipc -- -ipc SOCKET COMMAND`
executes the matching native `gsr-cli` directly. For example:

```sh
layer gsr-ipc -- -ipc "$XDG_RUNTIME_DIR/gsr.sock" save-replay 30
```
 Layer's recorder producers do not expose an unmanaged socket.
The source of truth is the bundled `src/args_parser.c`,
`gpu-screen-recorder.1`, `tools/gsr-cli/main.c` and `gsr-cli.1`; upstream
GPU/driver limitations still apply.
