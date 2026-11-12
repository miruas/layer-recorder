# Layer portability audit

Implementation review for the Layer 0.8 source, updated 2026-11-11. This supersedes the earlier read-only 0.7 survey. Source support, packaging and tested hardware are separate claims.

## Current result

The capture engine is the pinned Layer GSR fork, with Rust supervision and a single native Rust archive/editor. Python remains a headless configuration and media-processing worker; Qt/PySide is no longer part of the Layer GUI. GSR owns source discovery and hardware capture. Hyprland is an optional identity, geometry and shortcut integration.

The [Flatpak recipe](FLATPAK.md) packages a common Freedesktop runtime, both Rust executables, the fork, patched overlay and media workers. It addresses host Python/FFmpeg/libc version differences without declaring every distribution certified. Native packages/source builds retain their own host dependency requirements. Release assets and checksums must be checked on the actual release page; a recipe in the source tree is not proof that a binary was published.

## Runtime and packaging boundaries

| Area | Current implementation | Remaining boundary |
| --- | --- | --- |
| GUI | `native/editor`: eframe with X11 and Wayland, one archive/editor window. | Requires a working graphics/session stack. Software-rendered VM UI checks are not GPU capture checks. |
| Python | Python >=3.12; Pillow, NumPy and headless OpenCV for workers. | Native installations need compatible packages; Flatpak carries its own runtime/dependencies. |
| FFmpeg | Capture uses the fork's pinned stack; editor uses its FFmpeg compositor. Flatpak has a separate full editor CLI build. | Native builds must provide needed codecs/filters. HDR capture names alone do not establish HDR editing correctness. |
| Fork extensions | Independent node selection, first-packet timestamps and segment muxing. | Stock GSR cannot replace the fork. A binary without the required extensions is rejected. |
| Audio | PipeWire dynamic per-application/role capture, or separate PulseAudio device tracks. | Pulse fallback has no per-app separation or dynamic device inventory. No audio device yields display-only capture with a warning. |
| KMS and global helpers | Matching helpers are bundled for the Layer Flatpak ID. | Direct Flatpak KMS/global device helpers follow system-install host paths; host authentication, drivers and device access still apply. User-install editor/portal support is a different path. |
| Replay | Private tmpfs/ramfs ring with at least 512 MB free at allocation. | Small container `/dev/shm` limits can prevent replay. No automatic SSD fallback. |
| Startup | systemd user integration plus XDG autostart; direct daemon fallback if the user service manager is unavailable. | Desktop login must supply the display/audio session environment. The installer does not rewrite compositor configuration. |
| Drivers | GSR/NVENC for NVIDIA, GSR/VA-API for AMD/Intel; Flatpak runtime/driver integration. | Actual codec support and permissions depend on hardware and drivers. No arbitrary cross-GPU capture guarantee. |
| CPU / architecture | Locked Rust builds and portable Flatpak build tooling. | Current Flatpak recipe builds the host architecture; cross-compilation and every CPU baseline are not certified. |

See [Building](BUILDING.md), [Installation](INSTALLATION.md), [Flatpak](FLATPAK.md), [configuration](CONFIGURATION.md) and [notices](../NOTICE.md) for concrete requirements. Corresponding sources and linked-component licenses must accompany a published binary release.

## Desktop parity

| Session | Capture inventory | Identity / optional layout | Additional Layer key registration |
| --- | --- | --- | --- |
| Hyprland | GSR | `hyprctl` class/app identity and geometry. | Owned Hyprland bindings. |
| Sway | GSR | Sway tree app IDs and output geometry. | External user bindings. |
| X11, including GNOME/KDE X11 | GSR | EWMH `WM_CLASS` and XRandR. | External user bindings. |
| GNOME Wayland | GSR / explicit portal where supported | `Desktop`; synthetic horizontal layout. | External user bindings. |
| KWin/KDE Wayland | GSR / explicit portal where supported | `Desktop`; synthetic horizontal layout. | External user bindings. |
| Other Wayland sessions | GSR / explicit portal where supported | Safe fallback unless a supported adapter is detected. | External user bindings. |

GSR overlay shortcuts use GSR's separate machinery, including its desktop helpers. They must not be confused with Layer's optional Hyprland watcher. No standalone Layer GlobalShortcuts portal client is implemented. Missing or failed metadata probes do not remove GSR outputs or prevent capture. No window titles are read for recording-folder names. See [Desktop providers](desktop-providers.md).

## Validation performed and limits

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| Physical NVIDIA HEVC + independent Opus capture | Ordinary recording, RAM replay and editable independent sources on the development system; capture without a mandatory Hyprland API. | Physical AMD/Intel, every driver/codec, or every desktop. |
| Debian 13 Xfce/X11 and nested Weston | Packaged native UI startup and software-rendered interface checks. | Hardware encode, KMS or portal authorization. |
| openSUSE Leap 16 Xfce/X11 and nested KWin Wayland | Packaged native UI startup and software-rendered interface checks. | Hardware encode, full Plasma integration or every editor feature. |
| Python/Rust tests | Planning, model/timing behavior, socket lifecycle, synthetic segment retention, processing and startup guards within each test's scope. | Physical capture certification. Synthetic capture tests deliberately do not read the desktop. |
| Optional NVENC/VA-API export tests | Only establish an encoder path when explicitly enabled on working hardware. | A compositor capture path or a different GPU generation. |

The VM candidate was tested before the latest voiceover, batch-export and timeline UI additions; those additions need the final candidate's regression run. Ubuntu image startup did not complete, and no successful Ubuntu VM result is claimed. GNOME/Mutter, physical AMD/Intel capture, portal consent/restoration, V4L2 and focused-window capture remain explicit gaps.

## Remaining work before broad certification

1. Complete final-package regressions after the latest editor changes and record exact tested builds.
2. Exercise direct and portal capture on physical NVIDIA, AMD and Intel systems across X11, GNOME Wayland, KDE Wayland and a wlroots compositor.
3. Verify host-helper authorization and global shortcuts from a system-installed Flatpak, not only an editor-only user installation.
4. Keep HDR color-management limitations explicit; do not equate codec passthrough with HDR editor parity.
5. Direct display hotplug now reconciles GSR inventory in a background worker, preserving older source generations. Broader native Wayland identity/layout adapters and independent portal shortcuts remain future work and must preserve the fallback and privacy guarantees.

A distribution may run the common Flatpak runtime while a particular capture source remains unavailable because of GSR, drivers, permissions or portal support. That is the support boundary, rather than a promise that all host/session combinations were tested.
