# Changelog

## 0.8.0 (unreleased)

- Add validated advanced GSR capture controls, all source modifiers and exact native CLI passthrough alongside managed independent-source recording.
- Add native Rust original-video quick export for eligible complete clips, with independent audio mixing and conservative render fallbacks for edits/cuts.
- Simplify proven redundant export filters and expose codec-specific render speed controls without changing default quality.

- Keep privacy effects, titles and supported libass captions in bounded GPU overlay caches; retain the last complete preview while new glyphs or frames load.
- Default to Native/source preview quality with configurable 1080p/1440p/4K/custom bounds and 60/120 FPS scheduling; discover missing legacy source rates in the background.

- Replace the Qt archive/editor with one native Rust window; move loading, font discovery, waveform analysis, playback teardown and media work off the interface thread.
- Preserve version-1 independent-source recordings and editing, with native voiceover, batch export, timeline zoom/snapping and real audio waveforms.
- Read shared GSR recording/replay preferences and expose configurable short/long replay durations and editor shortcut in the normal GSR overlay.
- Separate optional desktop identity providers from GSR capture; add PulseAudio device capture when dynamic PipeWire application discovery is unavailable.
- Add a Freedesktop 26.08 Flatpak recipe with pinned static host helpers, full editor FFmpeg, speech executable and legacy Intel driver; never bundle speech models or user configuration.
- Add Debian/openSUSE VM tooling and X11/Wayland startup regression checks. See the compatibility matrix for tested cases and outstanding hardware coverage.
- Report capture startup failures in status and preserve ordinary GSR startup/shortcut behavior.
- Coalesce autosave off the interface thread, gate media jobs on persisted source metadata, and flush edits asynchronously on close with recoverable save failures.

## 0.7.0

- Rust capture daemon, private RAM allocator, segment retention/spool and independent-source snapshots.
- GSR-only video and audio producers; removed automatic wf-recorder fallback and pw-record/FLAC producer pipeline.
- Event-driven PipeWire discovery, bounded control queue and responsive cached status during saves.
- Optional Hyprland adapters and explicit GSR portal/X11-window/camera capture targets, with documented validation gaps.
- Fork extensions for exact PipeWire audio-node selection, audio timestamps and correct Opus selection in segmented Matroska.
- Preserved native editor, JSON socket and version-1 project compatibility.


## 0.6.0

- Native Recording, Replay, Shortcuts and GSR Integration settings.
- Explicit H.264/H.265/AV1, configurable RAM buffer and two replay presets.
- Editable owned Hyprland bindings, conflict reporting and selective backed-up GSR preference updates.
- Patched GSR overlay reloads shared hotkeys immediately.
- Compiled Arch/CachyOS x86_64 release package, corresponding sources, checksums and source-build guide.
- User-cache subtitle model downloads for read-only system installations.


## 0.5.1 — 2026-11-10

- Convert the standalone distribution into a GPU Screen Recorder source fork based on official tag6.1.7, preserving its full public Git ancestry and copyright notices.
- Include/build the native recorder core locally and expose explicit Layer fork provenance through its capability output.
- Use GPL-3.0-only for the fork distribution and first-party source metadata, retaining independent third-party notices.
- Make the locally built fork the default recorder; a system recorder remains an explicit configuration option.
- Sanitize upstream example paths and verify the original public documentation asset by hash.

## 0.5.0 — 2026-11-10

- Publish a portable, source-only Layer distribution with no personal configuration/media/binaries.
- Add automatic/NVIDIA/AMD/Intel profiles, strict JSON settings/schema and CLI validation/diagnostics.
- Add native GPU capture profiles and detected NVENC/VA-API export choices.
- Add AMD/Intel VA-API export graphs and current-FFmpeg hardware-filter initialization in the Wayland helper.
- Preserve RAM replay, independent sources, readable audio roles, full native Studio and optional GSR shortcut/menu mirroring.
- Add pinned helper recipes, optional checksum-verified speech-model download, user-local installer and English documentation.
- Add portable CI, hardware command tests, real opt-in VA-API checks and a public-tree release guard.

Earlier machine-local iterations established independent display/audio recording,60/300sec replay saves, native archive/Studio, silent application-named saves and bounded RAM storage. They are not included as public history because local working trees and configuration can contain private information.
