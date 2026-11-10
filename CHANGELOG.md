# Changelog

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
