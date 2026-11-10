# GPU support

Layer has one source tree with automatic detection and four configuration profiles. Separate vendor forks are unnecessary: shared editor/capture logic receives a vendor-appropriate command plan.

## Capture paths

**GPU Screen Recorder** handles an output directly when it advertises that output and the requested GPU profile matches its detected capture GPU. Layer asks for GPU encoding and disables CPU fallback. GPU Screen Recorder chooses the device associated with capture; Layer does not promise arbitrary cross-GPU GSR encoding.

**The patched wf-recorder helper** handles other outputs or an explicitly selected Wayland backend. NVIDIA uses `h264_nvenc`, `hevc_nvenc` or `av1_nvenc`. AMD and Intel use the corresponding VA-API encoders and an available matching render node. Source timestamps and closed segment lists keep independent media aligned.

Cross-GPU compositor/encoder setups may need `wf_no_dmabuf: true`; this uses a frame-copy path followed by hardware encoding. It is not a zero-copy performance claim. The helper patch initializes hardware filter contexts before FFmpeg initializes `hwupload`, fixing current-FFmpeg VA-API capture.

## Drivers

- NVIDIA: a working proprietary/open NVIDIA kernel driver plus matching userspace NVENC support. Use `nvidia-smi` and the driver's encoder-session report for diagnosis.
- AMD: the kernel `amdgpu` driver, Mesa VA-API support and a GPU with hardware encoding. Arch commonly uses `libva-mesa-driver`; package naming varies by distribution.
- Intel: the kernel driver and the appropriate VA-API media driver for the GPU generation, commonly `intel-media-driver` on modern hardware. Older Intel devices may require another driver and may not support all codecs.

`vainfo --display drm --device /dev/dri/renderD<number>` reports VA-API profiles. Encoding needs an encoding entrypoint; a decode-only profile is insufficient. A real short encode remains the definitive check. Device access follows the distribution's normal render/session permissions; Layer does not change device permissions or grant system capabilities.

## Profiles

| Setting | Automatic | NVIDIA | AMD | Intel |
| --- | --- | --- | --- | --- |
| `gpu_vendor` | `auto` | `nvidia` | `amd` | `intel` |
| Capture codec | H.264 | H.264 | H.264 | H.264 |
| Export default | CPU H.264 | NVENC H.264 | VA-API H.264 | VA-API H.264 |
| Render node | automatic | not required for NVENC | matching AMD node | matching Intel node |

A missing explicit vendor/device fails clearly. `render_device` applies to VA-API helper capture/exports; it does not override GSR's device choice. The native export dialog lists encoder families present in the FFmpeg build and detected hardware; actual codec support is checked by the encoder when it starts.

## Verification boundaries

This version was tested with real NVIDIA capture/NVENC exports and real AMD VA-API H.264/HEVC exports. AMD Wayland VA-API capture produced a playable independent-source project without capture warnings. Intel profile/device selection and command paths are tested, but physical Intel hardware validation has not been performed. AV1 availability depends on generation; the tested AMD device did not advertise AV1 encoding, so AMD AV1 is not claimed as hardware-verified.

Upstream references: [GPU Screen Recorder](https://repo.dec05eba.com/gpu-screen-recorder), [wf-recorder hardware encoding](https://github.com/ammen99/wf-recorder#readme), [FFmpeg encoder documentation](https://ffmpeg.org/ffmpeg-codecs.html).
