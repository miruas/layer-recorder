# Configuration reference

Layer merges a JSON object with validated defaults. Unknown keys, invalid enum values and incorrect types are rejected. Missing keys inherit defaults. The [schema](../examples/settings.schema.json) is suitable for editor validation; the application also validates settings itself.

The default file is `${XDG_CONFIG_HOME:-~/.config}/katman/settings.json`. `LAYER_CONFIG` selects another file. Environment-specific values belong in that local file, not in a public repository.

| Key | Default | Meaning |
| --- | --- | --- |
| `library` | `~/Videos/Layer` | Durable saved-project directory; `~` expands to the local home directory. |
| `fps` | `60` | Capture FPS, integer 1–240. High rates need suitable hardware. |
| `bitrate` | `18000` | Video bitrate per display in kbps, 1000–200000. Multiple screens add their bitrates. |
| `buffer` | `300` | RAM ring length in seconds, 2–86400. |
| `replay_short` | `60` | Short save duration in seconds; 1–86400 and no longer than the long preset. |
| `replay_long` | `300` | Long save duration in seconds; no longer than the buffer. |
| `shortcuts_enabled` | `true` | Register Layer-owned global shortcuts with Hyprland. |
| `shortcuts` | see below | Editable `ui`, `record`, `replay`, `save-1`, `save-5` combinations; empty disables an action. |
| `gsr_sync` | `false` | Selectively sync capture codec and replay length into existing GSR preferences on Save. |
| `auto_replay` | `false` | Start replay when the capture service starts. |
| `gpu_vendor` | `auto` | `auto`, `nvidia`, `amd`, `intel`. An explicit missing vendor fails. |
| `capture_backend` | `auto` | `auto` or `gsr`; both use GSR exclusively. |
| `capture_targets` | `[]` | Empty: every GSR-enumerated monitor. Explicit individual monitor names, `portal`, X11 numeric window ID / `focused`, or V4L2 device path. Advanced targets retain upstream requirements; no combined `|` targets. |
| `capture_codec` | `h264` | `h264`, `hevc`, `av1`; requires hardware support. |
| `render_device` | `auto` | VA-API render node, or `/dev/dri/renderD<number>`. Used for AMD/Intel. |
| `wf_no_dmabuf` | `false` | Deprecated; must remain false. The wf-recorder fallback was removed. |
| `export_codec` | `libx264` | Initial encoder selection in the native export dialog. See supported values below. |
| `export_quality` | `20` | Initial quality value, 0–51; lower means higher quality. Encoder scales differ. |
| `gsr_binary` | `auto` | Use the local Layer GSR fork. An explicit executable name/path opts into a system/custom recorder. |
| `wf_recorder_binary` | empty | Deprecated; must remain empty. |
| `speech_binary` | empty | Optional whisper.cpp CLI path; empty uses the local speech build. |
| `speech_model` | empty | Optional local ggml model path; empty uses the downloaded model in the user cache, then the legacy `speech/ggml-base.bin`. |

Export codec values: `libx264`, `libx265`, `libaom-av1`, `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`, `h264_vaapi`, `hevc_vaapi`, `av1_vaapi`. Export settings can override the initial selection for one export; preview/proxy jobs use their own CPU encoding settings.

```sh
layer config show
layer config validate
layer config init --profile intel
```

`init` refuses to replace an existing file. To start from an example, copy it to your own configuration directory only after checking existing preferences. Reinstalling never replaces settings. Native common preferences preserve advanced keys.

Capture changes apply on the next engine start. Save an ordinary recording and any desired replay before restarting the service. A fresh ring fills from that point; saved projects remain unchanged.

## Memory and storage

There is no disk fallback for replay. Layer requires a private memory-backed `/dev/shm` directory and checks free space. The ring retains the configured buffer duration plus segment safety margin. Ordinary recordings retain older closed segments on disk so recording duration does not grow the RAM ring indefinitely. Logs/metadata and the OS can add overhead.

At 18000 kbps, one video stream is approximately 675 MB for five minutes before audio and overhead. Multiple displays and independent Opus audio add usage. Monitor your actual memory/storage for the chosen setup.

## Shared GPU Screen Recorder preferences

The normal GSR UI is the capture settings editor. Layer reads its `config_ui`
without rewriting it: replay/record codec, frame rate, quality preset or custom
bitrate, replay buffer and short/long save durations. Capture quality never
changes export CRF. The active capture session keeps its format until stopped
and restarted; ordinary recordings are not interrupted by settings edits.

| Key | Default | Meaning |
| --- | --- | --- |
| `gsr_preferences` | `true` | Read canonical GSR preferences; `false` uses Layer's capture values. |
| `gsr_source` | `gsr` | Advanced source selector: `gsr`/`auto`, or `layer`/`off` to opt out. |
| `gsr_config` | empty | Explicit GSR config file, unless `LAYER_GSR_CONFIG` is set. |
| `capture_quality` | `custom` | `medium`, `high`, `very_high`, `ultra` use GSR QP presets; `custom` uses CBR bitrate. |

`LAYER_GSR_CONFIG` has priority. Otherwise Layer selects native GSR's config,
then an existing original GSR Flatpak config. The bundled overlay edits that
same selected file. Flatpak does not create a disconnected copy. Unrelated GSR
fields remain intact. The legacy `gsr_sync` flag only controls the explicit
write helper; it is separate from default read integration.

HEVC/AV1 10-bit and HDR codec names are preserved exactly during capture and
checked against GSR's advertised hardware codecs. HDR editing/color management
is not certified; keep original media for color-managed workflows.

## Shortcuts and startup

Normal GSR global settings provide shared record/replay/save keys and an
**Open Layer editor** shortcut, default Meta+Alt+Z. Alt+Z retains the overlay.
Short/long durations are configurable in GSR replay settings, in seconds,
within the buffer. Older config files without these fields inherit Layer's
presets, shortened to fit a smaller buffer. `layer save-1` and `layer save-5`
remain compatibility command names and use the configured durations.

The `shortcuts` object contains optional Hyprland-owned bindings. Other desktop
shortcut tools can bind the same CLI commands; capture does not depend on that
adapter. `python install.py --enable` also installs a standard XDG autostart
entry, so startup works without systemd. Set `auto_replay` to true, or let
GSR's successful replay action start the shared Layer ring.

The public editor uses a neutral native palette. Personal theme-manager
integration is outside the public source tree.

### Capture resolution limits

Recording and replay follow their own GSR resolution controls. In each GSR
settings page, choose **Native**, **720p**, **1080p**, **1440p**, **4K**, or
**Custom**. A preset is an upper bound: the capture keeps its aspect ratio and
never enlarges a smaller source. For example, a 2560 × 1600 screen limited to
1080p produces 1728 × 1080, while a 1280 × 720 screen stays 1280 × 720.
Custom bounds are rounded down to even pixels. The focused-window area control
continues to specify its capture area separately.

The presets use GSR's existing public schema, with no additional GSR keys:

```json
{
  "record": {
    "record_options": {
      "change_video_resolution": true,
      "video_width": 1920,
      "video_height": 1080
    }
  },
  "replay": {
    "record_options": {
      "change_video_resolution": true,
      "video_width": 1920,
      "video_height": 1080
    }
  }
}
```

Set `change_video_resolution` to `false` for Native. Resolution settings do not
fall back from recording to replay or from replay to recording. Missing settings
retain Layer's configured default (Native by default). Codec, quality, bitrate,
and frame rate continue to follow their existing GSR mappings independently.

For standalone Layer configuration, set `"gsr_preferences": false` and use
`"capture_resolution": "native"`, `"720p"`, `"1080p"`, `"1440p"`, or `"4k"`.
Custom limits use `"capture_resolution": {"width": 1600, "height": 1000}`;
both dimensions must be even integers between 32 and 32768. This limits each
independent capture producer through GSR's `-s WIDTHxHEIGHT`, including portal
sources whose dimensions become available only when capture starts. Known
physical source dimensions additionally clamp the requested bounds before
launch. Layout metadata retains its original placement and source dimensions;
the saved encoded track is probed for its actual size. Capture resolution is
independent of export quality and export dimensions.

### Preview quality and frame rate

Preview preferences are independent of recording, replay, and export settings:

```json
{
  "preview_resolution": "native",
  "preview_fps": "source"
}
```

`preview_resolution` accepts `native` (default), `1080p`, `1440p`, `4k`, or a
custom object such as `{"width": 1600, "height": 1000}`. Custom dimensions must
be even integers between 32 and 32768. These are bounds for the **combined
canvas**, preserving its aspect ratio and never enlarging smaller content.
Native keeps the canvas resolution up to the preview safety limit of 3840 ×
2160; all preview modes share this limit to bound memory use. For example,
Native preserves two side-by-side 1920 × 1080 sources at 3840 × 1080, while
1080p limits their combined canvas to 1920 × 540. Output dimensions round down
to even pixels. Source media remains unchanged.

`preview_fps` accepts `source` (default), `60`, or `120`. Source follows the
highest known frame rate of enabled video sources, then the recording's frame
rate, then the editor canvas frame rate; missing metadata falls back to 60.
Preview cadence is limited to 120 FPS. Source frame rates such as 59.94 and
119.88 round to 60 and 120 for scheduling. Selecting 120 for a 60 FPS source
can repeat frames; it does not synthesize additional motion detail or change
the recording FPS. Higher-quality previews require more decoding and drawing
work. These preferences never reduce capture FPS or alter export quality.
