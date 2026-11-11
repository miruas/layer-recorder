# Flatpak build and installation

The Flatpak recipe packages the native Rust library/editor, Rust recorder core,
the pinned Layer GSR fork, the patched GSR overlay, and headless media workers in
one Freedesktop runtime. The host does not need the same Python, FFmpeg, Qt or
C library versions as the build machine. Qt is not used by the Layer interface.

This is a packaging path, not a certification that every distribution, desktop,
GPU or capture source has been tested. Actual session checks and remaining
hardware coverage are documented in [Testing](TESTING.md) and
[GSR compatibility](GSR_COMPATIBILITY.md). The current recipe builds for the
host architecture; cross-compilation is not implemented.

## Install a compiled bundle

Install Flatpak using your distribution's package manager, then add Flathub for
the required runtime:

```sh
flatpak remote-add --user --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo
```

For a release that provides a `.flatpak` asset, verify its published checksum.
The filename below is illustrative; use the actual release asset:

```sh
sha256sum -c SHA256SUMS
flatpak install --system ./io.github.miruas.Layer-VERSION-x86_64.flatpak
flatpak run io.github.miruas.Layer
```

Direct KMS capture and the GSR global input helper follow upstream's host-helper
model. The matching static helpers are bundled under the Layer application ID.
Their host paths use the **system** Flatpak installation. Authentication and
hardware permissions remain the host's responsibility; the build does not grant
capabilities, modify drivers or install privileged files itself.

A user installation can be used for the editor and a supported portal capture
path:

```sh
flatpak install --user ./io.github.miruas.Layer-VERSION-x86_64.flatpak
flatpak run io.github.miruas.Layer config init
```

For portal capture, set `capture_targets` to `["portal"]` in Layer's configuration
and grant the desktop's screen-selection request. Portal availability, session
restore and supported buffer formats depend on GSR and the desktop portal.
User installation alone does not provide the system host-helper path for direct
KMS or global device shortcuts.

## Switching from an existing native installation

Layer installations share the capture control socket. Finish an ordinary recording
and stop the older Layer capture service before testing another installed version;
otherwise the new editor may control the existing daemon. Keep one capture
autostart entry active. Existing recording folders remain intact, while the
Flatpak has its own application configuration directory and uses the shared GSR
preferences described below. Personal local themes are not part of this neutral
portable preview.

## Build from source

Requirements on the build host are Git, Python 3.12 or newer, Flatpak and the
Freedesktop SDK. Build tools and media libraries come from that SDK. Fetch the
repository with its pinned sources and install the SDK components:

```sh
git clone https://github.com/miruas/layer-recorder.git
cd layer-recorder
flatpak remote-add --user --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub \
  org.freedesktop.Sdk//26.08 \
  org.freedesktop.Platform//26.08 \
  org.freedesktop.Sdk.Extension.rust-stable//26.08
python3 tools/build-flatpak.py --check
```

Build the complete application in the SDK:

```sh
python3 tools/build-flatpak.py --jobs 4
```

The builder initializes its application directory, fetches and patches the pinned
GSR UI source, and builds the matching static KMS server, proxy and global input
helpers automatically. It verifies the pinned libdrm and proxy checksums. No
host compiler, capability grant or privileged file installation is needed for
the build itself.

Both Rust executables use locked Cargo dependencies. The recorder uses its
minimal static FFmpeg stack; the editor includes a separate full FFmpeg CLI with
PNG previews, text/subtitles, stabilization, software codecs and supported hardware encoders. The renderer build verifies these capabilities before packaging. Pillow and
NumPy are built from verified pinned source distributions against SDK libraries.
NumPy has a generic CPU baseline and no external BLAS/Fortran payload. OpenCV is built from pinned source without its own
FFmpeg decoder. The package also contains the speech executable and an explicit
model-download command; speech weights are downloaded to the user's cache only
when requested with `layer download-model`.

Builds use a neutral source path inside the SDK so private checkout paths are
not embedded in published binaries. No recording, user setting, personal theme,
credential or speech model is staged.

The default outputs are an OSTree repository, a `.flatpak` bundle and its
`SHA256SUMS` under `dist/flatpak/`. `--output` and `--repo` change those locations.
Use `--jobs` to bound compilation memory and `--offline` to disable GSR subproject
downloads after the source cache has been populated. `--help` lists all options.
`--clean` removes the selected build directory and rebuilds its cached components.

A binary release must include complete corresponding sources and the original
licenses for all linked components, including the exact GSR/UI patches,
FFmpeg stack, Rust crates, libdrm, proxy and bundled libcap. A successful bundle
build by itself is not evidence that this source-distribution requirement has
been completed. Refer to [Upstream provenance](../UPSTREAM.md) and
[Notices](../NOTICE.md) before publishing.

## Shared GSR preferences

The launcher chooses an existing native GSR `config_ui`, then an existing
original GSR Flatpak `config_ui`, unless `LAYER_GSR_CONFIG` selects a file
explicitly. The bundled overlay reads and writes that same selected file.
Layer does not seed a divergent copy or overwrite unrelated preferences.
If no existing file is selected, the bundled GSR overlay uses its app-local
configuration directory.

The read/write configuration permissions are deliberate: recording preferences
and configurable replay durations remain editable through the normal GSR UI.
The headless Layer settings reader only reads those preferences. Layer's library
and export settings remain separate. A setting that changes capture takes effect
on the next capture start or an explicit settings reload; an ordinary recording
must be stopped before its capture configuration can be reloaded.

## Desktop and driver requirements

- GSR owns monitor discovery and GPU capture. Use a capture source that the
  installed GPU driver and session advertise.
- Independent application/role audio uses PipeWire. With native PulseAudio,
  Layer captures the enumerated microphone and output devices separately;
  per-application separation is unavailable there. A display-only session can
  record without an audio device, with an explicit warning.
- NVIDIA requires a compatible host driver and Flatpak NVIDIA GL extension.
  AMD/Intel require a supported VA-API driver and accessible render device.
  An encoder appearing in an FFmpeg list does not prove hardware support.
- Native desktop global shortcuts and portal consent policies differ. The GSR
  overlay owns its normal shortcuts. Layer CLI actions can also be bound through
  the desktop's shortcut settings.
- Replay storage is RAM-backed and requires sufficient available `/dev/shm`
  capacity. This recipe does not switch replay to disk silently.

For login startup, add `flatpak run io.github.miruas.Layer autostart` in the
desktop's startup-application settings. This starts/reuses the recorder service;
it does not open a second editor window. Replay still follows the configured
startup policy.

## Troubleshooting

Run the bundled capability report from the same desktop session:

```sh
flatpak run io.github.miruas.Layer doctor
flatpak run io.github.miruas.Layer status
```

If direct capture cannot start, check the system versus user installation,
matching helper versions, device access and any pending authentication dialog.
Try an explicitly selected portal source where GSR supports it. Do not bypass
the host's permission policy by granting arbitrary capabilities to a downloaded
binary. UI tests in a VM with a software renderer validate launch and interaction;
they do not validate NVENC, VA-API or physical display capture.

The permission model follows the
[upstream GSR Flatpak recipe](https://github.com/flathub/com.dec05eba.gpu_screen_recorder),
with Layer-specific application paths and shared GSR configuration access.

## Matching corresponding sources

After committing the reviewed public source and building the matching binary,
create its source asset on the build host (Cargo must be on `PATH`):

```sh
python3 tools/check-public-tree.py
python3 tools/build-flatpak-sources.py \
  --cargo-home build/flatpak08/cargo --fetch-sdists
```

The command verifies the binary's clean-commit manifest, preserves exact helper
snapshots and patches, vendors both locked Rust crates, and includes original
verified dependency archives plus Python source distributions. The output has a
source manifest, per-file hashes, rebuilding instructions and a separate checksum.
It contains no recordings, user settings, speech weights or compiled application.
Only explicitly reviewed non-Linux binary fixtures are omitted from vendored
crates, with every omission and checksum change documented. `--development`
creates an explicitly marked private QA archive; do not publish it as a release.

Publish the matching `.flatpak`, corresponding-source `.tar.xz`, and checksums
together. The recipe records the runtime, SDK and Rust extension commits as
separately distributed platform components. The included `REBUILD.md` explains
restoring the helper snapshots and offline Rust source configuration.
