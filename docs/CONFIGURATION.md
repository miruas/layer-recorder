# Configuration reference

Layer merges a JSON object with validated defaults. Unknown keys, invalid enum values and incorrect types are rejected. Missing keys inherit defaults. The [schema](../examples/settings.schema.json) is suitable for editor validation; the application also validates settings itself.

The default file is `${XDG_CONFIG_HOME:-~/.config}/katman/settings.json`. `LAYER_CONFIG` selects another file. Environment-specific values belong in that local file, not in a public repository.

| Key | Default | Meaning |
| --- | --- | --- |
| `library` | `~/Videos/Layer` | Durable saved-project directory; `~` expands to the local home directory. |
| `fps` | `60` | Capture FPS, integer 1–240. High rates need suitable hardware. |
| `bitrate` | `18000` | Video bitrate per display in kbps, 1000–200000. Multiple screens add their bitrates. |
| `buffer` | `300` | RAM ring length in seconds, 30–3600. |
| `replay_short` | `60` | Short save duration in seconds; 1–3600 and no longer than the long preset. |
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

## Themes

No personal system theme files are bundled. When an existing theme-manager is present under the user's configuration, Layer reads its selected preset and semantic palette roles live. Otherwise it uses a built-in native dark palette. Theme files are never modified by Layer.

## Native settings

Open **Settings** from the recording library. Recording exposes H.264 (AVC), H.265 (HEVC), AV1, GPU profile and the default export encoder. Replay exposes the RAM buffer, both save presets and startup behavior. Selecting **Apply recording changes now** first saves the available Layer replay, then restarts idle capture; active ordinary recordings are rejected without interruption.

Shortcuts are single combinations, written in Qt portable form (for example `Meta+Alt+Z`). Defaults: Open Layer `Meta+Alt+Z`, record `Meta+Alt+F9`, replay `Meta+Alt+F10`, short save `Meta+Alt+F7`, long save `Meta+Alt+F8`. Clear a field to disable it. Only owned Hyprland bindings are changed. Conflicting compositor bindings are reported before saving. New settings are reapplied within five seconds and after a compositor reload.

The GSR Integration tab edits the existing overlay, record, replay and save shortcuts. With synchronization enabled, Save also updates GSR's normal/replay codec and buffer length, retaining unrelated settings and keeping save notifications disabled. GSR preferences are backed up under the private user state directory. The bundled patched overlay reloads keys immediately; an unpatched overlay may need restarting. Codec changes apply at GSR's next capture start. Layer does not interrupt GSR captures automatically.

GSR's native short video save remains one minute, and its full save uses its own ring length. Mirrored Layer saves use the configurable short/long presets. These outputs are separate.
