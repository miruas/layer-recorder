# Configuration reference

Layer merges a JSON object with validated defaults. Unknown keys, invalid enum values and incorrect types are rejected. Missing keys inherit defaults. The [schema](../examples/settings.schema.json) is suitable for editor validation; the application also validates settings itself.

The default file is `${XDG_CONFIG_HOME:-~/.config}/katman/settings.json`. `LAYER_CONFIG` selects another file. Environment-specific values belong in that local file, not in a public repository.

| Key | Default | Meaning |
| --- | --- | --- |
| `library` | `~/Videos/Layer` | Durable saved-project directory; `~` expands to the local home directory. |
| `fps` | `60` | Capture FPS, integer 1–240. High rates need suitable hardware. |
| `bitrate` | `18000` | Video bitrate per display in kbps, 1000–200000. Multiple screens add their bitrates. |
| `buffer` | `300` | Fixed five-minute ring. Keep 300; save actions select 60 or 300 seconds. |
| `auto_replay` | `false` | Start replay when the capture service starts. |
| `gpu_vendor` | `auto` | `auto`, `nvidia`, `amd`, `intel`. An explicit missing vendor fails. |
| `capture_backend` | `auto` | `auto`, `gsr`, `wf-recorder`. Auto prefers compatible direct GSR capture. |
| `capture_codec` | `h264` | `h264`, `hevc`, `av1`; requires hardware support. |
| `render_device` | `auto` | VA-API render node, or `/dev/dri/renderD<number>`. Used for AMD/Intel. |
| `wf_no_dmabuf` | `false` | Force the helper's frame-copy path; useful for cross-GPU import problems. Encoding remains hardware accelerated. |
| `export_codec` | `libx264` | Initial encoder selection in the native export dialog. See supported values below. |
| `export_quality` | `20` | Initial quality value, 0–51; lower means higher quality. Encoder scales differ. |
| `gsr_binary` | `auto` | Use the local Layer GSR fork. An explicit executable name/path opts into a system/custom recorder. |
| `wf_recorder_binary` | empty | Optional explicit path/name of the patched helper; empty uses the local build. |
| `speech_binary` | empty | Optional whisper.cpp CLI path; empty uses the local speech build. |
| `speech_model` | empty | Optional local ggml model path; empty uses `speech/ggml-base.bin`. |

Export codec values: `libx264`, `libx265`, `libaom-av1`, `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`, `h264_vaapi`, `hevc_vaapi`, `av1_vaapi`. Export settings can override the initial selection for one export; preview/proxy jobs use their own CPU encoding settings.

```sh
layer config show
layer config validate
layer config init --profile intel
```

`init` refuses to replace an existing file. To start from an example, copy it to your own configuration directory only after checking existing preferences. Reinstalling never replaces settings. Native common preferences preserve advanced keys.

Capture changes apply on the next engine start. Save an ordinary recording and any desired replay before restarting the service. A fresh ring fills from that point; saved projects remain unchanged.

## Memory and storage

There is no disk fallback for replay. Layer requires a private memory-backed `/dev/shm` directory and checks free space. The ring retains approximately five minutes plus segment safety margin. Ordinary recordings retain older closed segments on disk so recording duration does not grow the RAM ring indefinitely. Logs/metadata and the OS can add overhead.

At 18000 kbps, one video stream is approximately 675 MB for five minutes before audio and overhead. Multiple displays and FLAC audio add usage. Monitor your actual memory/storage for the chosen setup.

## Themes

No personal system theme files are bundled. When an existing theme-manager is present under the user's configuration, Layer reads its selected preset and semantic palette roles live. Otherwise it uses a built-in native dark palette. Theme files are never modified by Layer.
