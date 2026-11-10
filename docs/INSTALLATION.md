# Installation

## Supported environment

Layer capture currently requires Linux, a Hyprland Wayland session, PipeWire and a supported hardware encoder. The native editor and ordinary CPU exports do not require a capture GPU or a running compositor.

Use Python 3.12 or newer. Current capture/helper builds target FFmpeg 8.1 or newer. The editor's filter-file handling also supports older FFmpeg builds used in CI. Distribution packages must include the filters/encoders you use: libass, drawtext, vid.stab, libx264/libx265 and optional AV1/VA-API/NVENC.

## Arch / CachyOS

Install packages using your normal package manager. A typical runtime/editor set is:

```sh
sudo pacman -S --needed python python-pip python-pyside6 python-numpy python-opencv \
  ffmpeg pipewire libpulse fontconfig noto-fonts libnotify
```

Install GPU Screen Recorder from the distribution's supported package or its upstream instructions. Confirm `gpu-screen-recorder --info` succeeds before troubleshooting Layer.

Native helper build dependencies include:

```sh
sudo pacman -S --needed base-devel git meson ninja cmake pkgconf \
  wayland wayland-protocols libdrm libglvnd libpulse libx11 libxcomposite \
  libxfixes libxext libxi libxcursor libxrandr libxrender libxkbcommon \
  dbus pango freetype2 libdecor
```

The relevant development libraries come with Arch packages. On other distributions, install their development-header equivalents. See [GPU_SUPPORT.md](GPU_SUPPORT.md) for vendor drivers.

## Source checkout

```sh
git clone https://github.com/miruas/layer-recorder.git
cd layer-recorder
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . meson ninja
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
