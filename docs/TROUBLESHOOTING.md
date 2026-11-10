# Troubleshooting

## Start with capabilities

Run `layer config validate`, `layer doctor`, `gpu-screen-recorder --info` and `ffmpeg -encoders`. Review output before sharing: it may reveal local program/device details. Share only the relevant sanitized error.

## Capture cannot start

- Confirm Hyprland and PipeWire are running in the same graphical session.
- Confirm the user service has the Wayland/Hyprland session environment.
- If GSR cannot enumerate/capture an output, build the patched Wayland helper or select `capture_backend: wf-recorder`.
- For AMD/Intel, inspect VA-API encoding entrypoints with `vainfo`. Check device/session permissions through your distribution's supported mechanism.
- A forced GPU profile must match available hardware. GSR itself chooses the capture device; use auto or a compatible Wayland path when the selected vendor differs.
- Cross-GPU DMA-BUF import trouble can require `wf_no_dmabuf: true`.

## Encoder appears but export fails

FFmpeg's encoder list shows compiled support, not the GPU's codec capabilities. Older hardware may lack HEVC/AV1, or the installed driver may not expose the requested profile/rate-control mode. Try H.264, inspect the actual error, or choose a CPU export deliberately. Layer does not silently switch failed hardware capture to CPU.

## Replay save is short or unavailable

Replay must be enabled first. After a restart it contains only newly captured footage until the ring fills. A source that starts later has a corresponding offset. Any unavailable tail is reported as a capture warning. Preserve wanted footage before restarting the service.

## RAM storage is unavailable

`/dev/shm` must be memory-backed with enough free capacity. Layer requires512MB headroom before starting and checks it during capture. Existing system/shared-memory usage can reduce capacity. No disk fallback occurs. Reduce capture bitrate/display count or free appropriate system resources; do not delete another application's shared-memory files.

## Missing or unwanted audio

New app streams are discovered while recording. Replay source lists reflect the selected time window and actual captured audio. Voice/media can be selected independently only when the application exposes separate PipeWire streams. Check role metadata and source availability. Do not replace passive capture links with routing changes that remove normal playback.

## Helper build fails

Install the development headers/toolchain listed in INSTALLATION.md. Current helper patches require current FFmpeg headers/APIs. If an earlier build attempt created `build/native/<component>`, inspect/remove only that local component build directory before retrying. Never remove system packages or user recordings as a build-cleanup step.

## Local captions or tracking fail

Install the optional speech CLI/model or set their paths. Recognition is local and imperfect. A privacy region needs enough visual texture to track; shorten the range or use manual keyframes if tracking is lost. Review the full result before publishing.

## Moving the checkout

Reinstall user-local links/services after relocation. A built overlay's resource prefix is tied to its local build location and should be rebuilt there. Do not copy another person's compiled helper or local service/configuration files.
