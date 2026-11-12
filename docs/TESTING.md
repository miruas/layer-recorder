# Testing and verification

## Portable checks

```sh
python -m unittest discover -s tests -v
xvfb-run -a python tools/smoke-ui.py
python tools/check-public-tree.py
```

Tests generate their own colored video/tone media in temporary directories. They do not require or distribute personal recordings. Use isolated XDG configuration/cache directories when testing on a machine with an existing installation.

Coverage includes decoded composition pixels, independent audio, cropping, privacy effects, transforms, keyframes, splits/trims/fades, speed/reverse/freeze, subtitles, source-preserving imports, proxies/stabilization, original-media hashes, RAM/spool continuity, source-window membership, PipeWire role metadata, vendor commands, settings validation and installer ownership/idempotence.

The native Rust startup smoke test isolates configuration and IPC, and checks normal startup/close, a slow font lookup and a blocked project read. It never starts real desktop capture. Screenshots are optional local QA output; none are bundled.

## Real hardware

```sh
LAYER_TEST_GPU=1 python -m unittest discover -s tests -v
LAYER_TEST_VAAPI_DEVICE=/dev/dri/renderD<number> \
  python -m unittest discover -s tests -v
```

Choose a VA-API render node supported by your driver. NVIDIA tests require working H.264/HEVC/AV1 NVENC support for the supplied matrix. VA-API tests require H.264/HEVC encoding. These tests intentionally fail on unsupported requested hardware/codecs; default CI skips hardware-only checks.

For release0.5.0, all61 local tests passed with NVIDIA and AMD hardware enabled. This includes actual NVENC video exports and AMD VA-API H.264/HEVC exports. A separate live AMD Wayland capture produced a7.46sec project with independent audio and no capture warning. The patched wf-recorder and GSR UI builds completed locally. Intel command/profile paths are unit-tested, with physical Intel validation still open. No new five-minute soak was performed for the cross-vendor packaging change.

CI runs CPU/media/configuration/UI/package checks on Python3.12–3.14. It has no hardware GPU and must not be described as hardware verification.

## Build and privacy checks

`python -m build` produces source and wheel distributions. Inspect the source archive before sharing a release. `tools/check-public-tree.py` audits tracked source paths/text, rejects binary files, machine home/media paths and common credential patterns. This is a release guard, not a guarantee that an arbitrary repository is free of sensitive content; review staged files too.

Optional native sources are pinned in `packaging/native-lock.json`. Patches are published, compiled outputs ignored. Rebuild after changing a patch, and repeat a real short capture on the affected backend before claiming support.


## Source-fork validation (0.5.1)

All65 local tests passed with hardware exports enabled after importing the official GSR6.1.7 history. Additional checks verify GPL-3.0-only metadata, the preserved upstream ancestor, default local-fork selection and capability provenance. The in-tree native recorder was compiled and produced a7.88sec live recording without capture warnings. The native `--info` output identifies Layer0.5.1, GSR6.1.7 and the exact upstream base commit. Source and wheel packages were built; source distributions include the recorder's native source and license. The GitHub checkout fetches full history so CI can verify ancestry.

## 0.7 Rust GSR-only migration

Run `cargo test --locked --manifest-path native/recorder/Cargo.toml`, build the core (`python tools/build-native.py recorder`), then run the Python suite. Rust tests cover private tmpfs allocation/symlink refusal, incremental graph/segment parsing, role precedence, retired-source windows, disk spooling and real FFmpeg concatenation of RAM plus spool. Python socket tests launch the real Rust daemon with explicitly synthetic CPU-generated pixels: idempotent bridge states, normal recording plus replay, save/reload preservation, shutdown saving, malformed requests and status responsiveness. They never capture the desktop.

A separate isolated live run verified actual NVIDIA GSR HEVC display capture and GSR Opus application/microphone segments, with independent version-1 project media, no capture warning and no Hyprland API environment. This does not certify other compositor sessions or physical AMD/Intel hardware under the new core. No new multi-hour soak is claimed.

## 0.8 portable Rust Studio candidate

The 2026-11-12 candidate uses one native Rust archive/editor and a headless media
worker. The local suite completed 149 Python tests with three opt-in hardware
checks skipped, plus 30 editor and 10 recorder Rust tests. A separate hardware
smoke check exercised HEVC export with the packaged FFmpeg on NVIDIA NVENC and
AMD VA-API. This does not certify Intel encoding or AMD/Intel desktop capture.

The same x86_64 Flatpak was installed in two KVM guests:

| Guest | Display sessions | Result |
| --- | --- | --- |
| Debian 13 | Xfce/X11 and nested Weston Wayland | 36 backend tests, one expected GPU-only skip; seven GUI checks; six startup regressions passed |
| openSUSE Leap 16 | Xfce/X11 and nested KWin Wayland | Same checks passed |

The backend suite covers ordinary recording/replay with synthetic pixels,
independent sources, worker previews, captions, stabilization, overlays, batch
exports, waveforms and tracking. GUI checks cover wide and 950-pixel layouts,
voiceover/export/settings dialogs and clean automatic closure. Startup checks
inject a three-second font lookup and a blocked project read; the slow-font cases render and close before that three-second lookup completes. These guests use software rendering, not GPU
passthrough. They do not validate portal consent, privileged KMS/global-input
helpers, GNOME/Mutter or physical Intel hardware. Native NVIDIA UI-driver stalls
are not ruled out by VM startup results.

Package tests found and fixed missing PNG/subtitle renderer capabilities and a
vid.stab binary-header parsing issue. The builder now requires the editor's
critical filters/codecs; stabilization analysis explicitly writes portable ASCII.
Run package integration tests with a disk-backed `TMPDIR`; a small tmpfs is
correctly rejected by the ordinary-recording spool capacity check.

The corresponding-source packer was exercised separately: all 22,093 files in
the development archive matched its manifest, and both Rust crates resolved
their Linux dependencies offline from the archive. Reviewed Windows/WASM binary
fixtures are omitted with original hashes and modified Cargo file checksums;
unknown compiled inputs block source packaging. Release archives must be rebuilt
from the clean commit used by the matching binary.

Additional Rust regressions exercise blocked FIFO autosave, 1,000 coalesced submissions with one retained pending model per document, retained failed snapshots, imported-source metadata barriers and cancellation before a gated media process starts.

## GSR integration and hotplug checks

The integration candidate completed 191 Python tests (three opt-in hardware skips), 35 editor Rust tests and 24 recorder Rust tests.

The GSR control dispatcher is tested with an executable local RPC fixture; it checks the actual argument list, errors, unavailable-overlay behavior, scope notes and every action used by the editor. Dispatch acknowledgement is deliberately separate from completed capture.

The real Rust daemon is exercised with synthetic FFmpeg producers: pause/resume with continuous RAM replay, export of paused recordings, resume after an insufficient unpaused duration, independent ordinary-record lifecycle inside GSR replay/streaming, and unrelated standalone recording ownership. Mutable GSR inventories exercise monitor addition, removal, mode changes and replug without deleting earlier media. A slow inventory query verifies that stop remains responsive. Pure reconciliation tests cover probe failures, retry backoff, static consent-based sources and strict inventory parsing. Optional geometry failure retains the last known placement when GSR's physical mode is unchanged.

The native editor was also checked in Debian 13 Xfce/X11 and nested Weston Wayland, and openSUSE Leap 16 Xfce/X11 and nested KWin Wayland, at 1440, 950 and 800 pixel widths, with normal/slow-font/blocked-project startup checks. These are software-rendered VM checks; they do not certify physical cable hotplug or an authenticated broadcast account.
