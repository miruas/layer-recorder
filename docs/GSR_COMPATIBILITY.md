# GSR compatibility and remaining gaps

This matrix describes Layer 0.7's GSR-only capture architecture against the **pinned GSR 6.1.7** source, commit `1d4946a16139b446e6308f741e9ba99f2ed5b3a7`. It is not a claim that every upstream feature has been exposed or physically tested. See [UPSTREAM.md](../UPSTREAM.md) for provenance and [Testing](TESTING.md) for validation boundaries.

## Capture and platform compatibility

| GSR feature | Layer integration | Limits / evidence |
| --- | --- | --- |
| NVIDIA, AMD, Intel on Wayland and X11 | Vendor/advertised-codec validation; GSR captures every selected video source | Physical migration validation uses NVIDIA. AMD/Intel command paths are tested; the new Rust/GSR-only path still needs physical AMD/Intel verification. [GSR README](../upstream/gpu-screen-recorder/README.md), [planner](../katman/hardware.py). |
| Direct monitor capture / KMS | One GSR process per monitor; independent editable tracks | Requires compatible system KMS broker and drivers. [platform discovery](../katman/platform_capture.py), [Rust capture](../native/recorder/src/capture.rs). |
| X11 monitor capture | GSR inventory replaces mandatory Hyprland inventory | X11 hardware/session testing remains open. |
| Portal / session restoration | Explicit `capture_targets: ["portal"]`; GSR presents its permission dialog | No automatic consent or invisible fallback. Portal-selected dimensions are read from saved media. Multiple portal selections are not claimed as separate Layer monitor tracks. Portal restore permissions remain controlled by the desktop. |
| X11 window ID / focused window | Explicit numeric target or `focused` passed to GSR | Source-level integration; not physically verified. No window picker yet. GSR focused capture has upstream sizing requirements. |
| Camera / V4L2 | Explicit `/dev/video…` target passed to GSR | Source-level integration; not physically verified. GSR camera exclusivity and format constraints apply; no camera picker/settings UI. |
| Multi-source native GSR composition | Layer deliberately preserves independent producers instead | Flattened native GSR composition is not selectable as a Layer capture mode. Composition happens in the editor. |
| Monitor positions, rotation, scale | Optional Hyprland geometry enrichment | Without it, placement is **synthetic side-by-side**, marked `layout_provider: synthetic`; GSR's monitor list supplies dimensions, not logical desktop placement. |
| Virtual Wayland monitors | Explicit portal path when available | A virtual output absent from GSR's direct inventory cannot be captured by its direct path. The wf-recorder fallback has been removed. |
| Display hotplug | Initial inventory only | Automatic video-source hotplug remains missing. Audio-source discovery is dynamic. |
| Arbitrary GPU selection / cross-GPU encoding | Not promised | GSR captures/encodes with the display GPU; Layer's vendor profile validates this constraint. |

The relevant upstream CLI formats and restrictions are in the [GSR manual](../upstream/gpu-screen-recorder/gpu-screen-recorder.1). Capture targets are advanced configuration; unsupported GSR sources fail visibly instead of switching engines or CPU encoding.

## Codec, color and timing features

| GSR feature | Layer status |
| --- | --- |
| H.264, HEVC, AV1 | Exposed; advertised hardware support checked; CPU capture fallback disabled. |
| VP8, VP9 | Not exposed in Layer settings/planning. |
| HEVC/AV1 HDR and 10-bit | Not exposed. The editor/export pipeline also needs explicit color-management work before HDR can be promised. GSR itself excludes HDR on X11/portal. |
| Experimental Vulkan encoding | Not exposed; upstream marks it experimental. |
| QP/VBR/CBR, quality presets, keyframe interval | Layer currently fixes CBR, configured bitrate and a one-second keyframe interval for editable segmented replay. |
| CFR/VFR/content frame mode, cursor, tune, yuv444, color range | Mostly upstream defaults; not independently exposed. |
| Opus, AAC, MP3 audio | Layer's GSR audio capture requests **Opus 256 kbps** in segmented Matroska. Metadata records the probed codec. Audio is now lossy Opus; the old external-FFmpeg FLAC producer was removed. |
| FLAC | Pinned GSR disables FLAC; Layer does not claim lossless capture in this path. |

Fork additions: exact `app:layer-node:<PipeWire-id>` selectors keep application roles separate without capturing every stream of an application; audio-only first-packet sidecars provide timing; segmented Matroska uses its inner-container codec compatibility so Opus is not silently replaced with AAC. See [PipeWire implementation](../upstream/gpu-screen-recorder/src/pipewire_audio.c), [recorder](../upstream/gpu-screen-recorder/src/recorder/recorder.c) and [Rust audio discovery](../native/recorder/src/audio.rs). An unmodified system GSR lacks these extensions and is rejected by this independent-track engine.

## Replay, audio and control

| GSR feature | Layer status / deliberate boundary |
| --- | --- |
| Independent device/application audio | GSR is the producer for microphones and each application/role group. Voice and media stay separate when PipeWire exposes separate streams. Changes retire the old generation and start a new one; original captured media is retained. |
| Default devices, inverse-app capture and mixed input selectors | Upstream features, not selectable Layer source policies. Layer discovers all application roles and microphones. |
| Native RAM/disk replay, reset-on-save | Layer uses its own Rust-managed segmented RAM ring to retain independently editable sources. Replay has no disk fallback; ordinary recordings spool older closed segments to disk. |
| Native audio-only replay | **Upstream does not support it.** Layer's GSR audio-only producers feed the Rust ring rather than using GSR `-r`. |
| Normal recording plus replay | Shared encoded segments; disabling either does not stop the other. |
| Native GSR socket / pause / variable saves | GSR has these APIs; Layer currently supervises producers with signals and exposes its own compatible JSON socket. Direct native-GSR IPC and capture pause remain missing. |
| Alt+Z overlay and original GSR shortcuts | Existing patched GSR UI bridge remains; normal/replay actions mirror to Layer, with separate original GSR and independent-track outputs. |
| Region capture, screenshots, live streaming / WHIP | Upstream features not exposed as Layer capture actions. Editor crop and PNG export are separate features. |

## Hyprland separation

The Rust capture, RAM storage, segment retention, project saving and local control protocol do not call `hyprctl`. [The metadata bridge](../katman/native_platform.py) keeps Python configuration and optional desktop integration outside that core:

- GSR is the authoritative capture inventory. Hyprland may enrich geometry.
- Focus naming uses Hyprland only when that session is present. Other sessions save under **Desktop**, avoiding a stale application name from an earlier Hyprland session. A compositor-neutral active-application provider is still missing.
- Layer global-shortcut editing currently registers keys only on Hyprland. Other desktops can bind `layer record`, `layer replay`, `layer save-1`, `layer save-5`, and `layer ui` with their own shortcut tools; the GSR overlay keys remain its responsibility. Portal global-shortcut support is not yet implemented.
- Service startup uses the desktop's standard graphical user-session target; it does not depend on a particular GSR service name.

Physical checks on the development machine exercised direct GSR HEVC video and separate GSR Opus application/microphone recordings with `HYPRLAND_INSTANCE_SIGNATURE` absent. That demonstrates removal of the **mandatory Hyprland API**, not certification of KDE, GNOME or X11 sessions. Those desktops, portal consent, camera capture and physical AMD/Intel runs remain explicit validation gaps.
