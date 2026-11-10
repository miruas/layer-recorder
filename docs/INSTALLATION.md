# Installation

## Supported environment

Layer capture currently requires Linux, a Hyprland Wayland session, PipeWire and a supported hardware encoder. The native editor and ordinary CPU exports do not require a capture GPU or a running compositor.

Use Python 3.12 or newer. Current capture/helper builds target FFmpeg 8.1 or newer. The editor's filter-file handling also supports older FFmpeg builds used in CI. Distribution packages must include the filters/encoders you use: libass, drawtext, vid.stab, libx264/libx265 and optional AV1/VA-API/NVENC.

## Ready-to-install Arch / CachyOS package

Download the `.pkg.tar.zst` and matching SHA256SUMS from [Releases](https://github.com/miruas/layer-recorder/releases). Version 0.6.0 targets **x86_64 Arch/CachyOS**, with one package for NVIDIA, AMD and Intel. It is not an AppImage or a package for every Linux distribution.

Keep the system fully updated; partial upgrades are unsupported. Install GPU Screen Recorder through your distribution/AUR workflow first: Layer uses its compatible, system-managed KMS broker for privileged display capture. Install the appropriate vendor drivers from [GPU support](GPU_SUPPORT.md). Then:

```sh
sha256sum --check --ignore-missing layer-recorder-0.6.0-SHA256SUMS
sudo pacman -U ./layer-recorder-0.6.0-1-x86_64.pkg.tar.zst
layer doctor
layer ui
```

The package contains the compiled Layer GSR engine, patched overlay, Wayland fallback and offline subtitle engine. Runtime dependencies are resolved by pacman; subtitle model weights are an optional separate download (`layer-download-model`). Neither installation nor an update replaces user preferences, records the desktop, or enables services automatically. FFmpeg SONAME dependencies prevent loading helpers against an incompatible ABI.

For startup, run `systemctl --user enable --now katman.service` and enable replay startup in Settings, or use the GSR bridge. Set up the optional overlay as described in [Integration](INTEGRATION.md).

If migrating from a source installation, save/stop active captures, disable its service and remove its owned installation links with that checkout's `python install.py --uninstall` before installing the package. Otherwise a user-local launcher/service can shadow the packaged version. Projects and local preferences remain intact. Removal: save/stop captures, disable the service, then `sudo pacman -R layer-recorder`.

[Build from source and build your own installable package](BUILDING.md).

## Arch / CachyOS

Install packages using your normal package manager. A typical runtime/editor set is:

```sh
sudo pacman -S --needed python python-pip pyside6 qt6-multimedia qt6-svg qt6-wayland python-numpy python-opencv \
  ffmpeg pipewire libpulse fontconfig noto-fonts libnotify
```

The recorder source is included as a real source fork. Build it with the command below. A compatible distribution-installed `gsr-kms-server` is required for direct KMS capture, or use the matching broker built from the fork through your normal system packaging workflow. See [UPSTREAM.md](../UPSTREAM.md).

Native helper build dependencies include:

```sh
sudo pacman -S --needed base-devel git meson ninja cmake pkgconf \
  wayland wayland-protocols libdrm libglvnd libpulse libx11 libxcomposite \
  libxfixes libxext libxi libxcursor libxrandr libxrender libxkbcommon \
  dbus pango freetype2 libdecor vulkan-headers libva libcap libxdamage
```

The relevant development libraries come with Arch packages. On other distributions, install their development-header equivalents. See [GPU_SUPPORT.md](GPU_SUPPORT.md) for vendor drivers.

## Source checkout

```sh
git clone https://github.com/miruas/layer-recorder.git
cd layer-recorder
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . meson ninja
python tools/build-native.py gpu-screen-recorder
python tools/build-native.py wf-recorder
python install.py --profile auto --enable
```

An editable installation keeps the Python application and locally built helpers in the same checkout. The installer records the current Python interpreter in user-local launchers and the service. Keep the virtual environment and checkout in place; rerun installation after moving them.

If using distribution Python packages instead, skip the virtual environment/pip application install and run the tools with that interpreter. Meson, Ninja and CMake must be on PATH.

The installer creates `~/.local/bin/layer`, a compatible `katman` launcher, desktop/MIME entries and `katman.service`. It does not start recording unless you request service startup and enable replay. It does not overwrite an unrelated installation's launchers or rewrite your Hyprland settings.

## First run

```sh
layer config validate
layer doctor
layer ui
layer replay
```

Use `--profile nvidia`, `amd` or `intel` for a new configuration, or keep `auto`. Existing settings remain intact. Add `~/.local/bin` to the graphical session's PATH for compositor shortcuts and the overlay bridge.

## Automatic startup

`python install.py --enable` enables the service under `graphical-session.target`. Set `auto_replay` to `true` in settings to begin buffering when it starts. `--start` starts an inactive service now and does not restart an active capture.

The session must activate `graphical-session.target` and supply its Wayland/Hyprland environment to the user service manager. Depending on desktop startup, import these variables using the compositor/session's normal systemd integration. Do not import unrelated secrets into the service.

The optional GSR overlay's replay-autostart bridge can start Layer replay instead; repeated state requests are idempotent.

## Optional offline subtitles

```sh
python tools/build-native.py whisper
python tools/download-model.py
```

These download pinned public source and a checksum-verified public multilingual base model. They do not upload audio. Model/executable paths can instead be supplied with `speech_binary` and `speech_model`. The binary/model stay ignored by Git.

## Optional GSR overlay

```sh
python tools/build-native.py gsr-ui
python install.py --overlay --gsr-service gsr-ui-hyprland.service
```

Use the actual name of your existing user GSR service. The local override points to the additional-menu build. Packaged binaries are preserved. Save/stop active GSR capture before restarting its service to activate the override. See [INTEGRATION.md](INTEGRATION.md).

## Remove

Save/stop recording and run `layer shutdown`, then:

```sh
python install.py --uninstall
```

Projects, preferences and caches are preserved. Remove manually added compositor include lines/bindings separately. Uninstall refuses unrelated files. If an overlay override was removed, restart GSR after saving/stopping its capture to return to its normal service command.
