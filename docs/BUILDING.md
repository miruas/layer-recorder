# Building Layer

## Application and native components

Follow [Installation](INSTALLATION.md) for Linux/Hyprland, PipeWire, GPU drivers and development dependencies. Install stable Rust and Cargo (Rust 1.85+). The repository preserves upstream GSR history and includes its modified source under `upstream/gpu-screen-recorder`. Optional native sources are pinned in `packaging/native-lock.json`; the build tool downloads those exact revisions and applies the checked-in patches.

```sh
git clone https://github.com/miruas/layer-recorder.git
cd layer-recorder
git checkout v0.7.0
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . meson ninja
python tools/build-native.py gpu-screen-recorder
python tools/build-native.py recorder
python tools/build-native.py gsr-ui
python tools/build-native.py whisper
python install.py --profile auto --enable
```

The first builds require internet access. `--reuse` rebuilds an existing source checkout without recloning or applying a patch twice. `--prefix` and `--destdir` control the GSR overlay's embedded resource prefix and staged installation. Do not move that overlay to a different prefix without rebuilding it. Builds do not replace system GSR binaries or grant privileges. A compatible distribution-installed KMS broker is required for direct capture.

Whisper is optional. Download its checksum-verified model with `python tools/download-model.py`; weights go into the user's cache, never the source tree. The packaged equivalent is `layer-download-model`. Models and recordings are not part of releases.

## Build a ready-to-install package

Use a Linux x86_64 machine with Git, Python 3.12+ and a working Docker daemon. The script clones the **committed HEAD** into a clean Arch build environment; commit local modifications first. Root access inside the build container does not grant capture privileges to installed files.

```sh
python3 tools/release-container.py
```

Outputs under `dist/`:

- `layer-recorder-<version>-1-x86_64.pkg.tar.zst`: compiled components plus the native Python/Qt app.
- `layer-recorder-<version>-corresponding-sources.tar.gz`: the exact application and patched native source trees, including bundled submodules and original licenses.
- `layer-recorder-<version>-SHA256SUMS`: hashes of both artifacts.

The build container supplies a generic build path and installed-package inventory, avoiding host home paths and host package lists in `.BUILDINFO`. Dependencies remain distribution packages rather than bundled copies of Qt/FFmpeg/drivers. Package dependencies include FFmpeg ABI versions from the compiled executables. Rebuild against a changed Arch ABI; do not bypass dependency checks. Containers are build/launch checks, not physical GPU certification.

The corresponding-source archive includes `layer-recorder/` and exact `external/gsr-ui`, `external/wf-recorder`, `external/whisper` trees. Standard builds use the pinned upstream checkouts; the external trees also permit inspecting/rebuilding the exact shipped patched sources. The checked-in recipes are the installation/build scripts for the release. Retain the complete archive when redistributing binaries.

## Validation

```sh
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python tools/smoke-ui.py
python tools/check-public-tree.py
```

Hardware capture tests are opt-in; see [Testing](TESTING.md). Native Settings supports editable Layer keys and existing GSR preferences without requiring a custom build.

The corresponding-source archive includes vendored Rust crates under `layer-recorder/native/recorder/vendor` and a portable `.cargo/config.toml`. From that crate directory, `cargo build --offline --locked --release` rebuilds the core without fetching dependencies. The normal Git checkout uses Cargo.lock and downloads the pinned crate versions on its first build.
