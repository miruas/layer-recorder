# Desktop and GPU Screen Recorder integration

## Shared settings and shortcuts

The bundled, patched GSR UI retains the normal Alt+Z menu and its recording/replay shortcuts. Its regular settings expose the Layer editor shortcut (default Super+Alt+Z), replay buffer, and configurable short/long save durations. Defaults are 60 and 300 seconds; these are preferences rather than fixed command semantics.

Layer's Recording preferences opens the normal GSR replay/settings page. The shared GSR `config_ui` is the default capture source for codec, quality, FPS, bitrate and replay lengths; Layer reads it without rewriting unrelated options. Library and export preferences stay separate. Exact `auto`, HEVC/AV1 10-bit and HDR codec names are retained, subject to hardware support and the editor's documented color-management limits.

An explicit `LAYER_GSR_CONFIG` selects a file; Layer's advanced `gsr_config` can also select its reader's source. Otherwise the canonical reader prefers an existing native XDG GSR file, then an existing original GSR Flatpak file. The Layer Flatpak launcher selects a shared existing file for both components. See [Configuration](CONFIGURATION.md) and [Flatpak](FLATPAK.md). Capture-format changes take effect on the next capture/reload; an active ordinary recording must be saved/stopped first.

## Mirrored normal GSR actions

The patched GSR normal-record, replay-enable and replay-save handlers invoke the detached Layer bridge. Mirroring uses explicit states rather than toggles, preventing repeated overlay messages from accidentally reversing capture. The shared short/long durations determine save lengths.

GSR's ordinary output and Layer's independent-track output are separate capture sessions. Mirroring retains the usual GSR recording while also creating an editable Layer project, with additional encoder/memory use. Layer does not reuse the internal GSR replay buffer. The native editor capture toolbar now dispatches GSR's canonical RPC commands rather than silently toggling the independent recorder. The Capture menu exposes live broadcast, region recording, and full-screen/region/window screenshots. GSR owns the network stream, interactive region/window selection, and image destination. A successful command acknowledgement is not proof that the capture or broadcast completed. GSR errors remain visible in its overlay.

Ordinary recording inside an active GSR replay or streaming session has its own mirrored start/stop lifecycle. Ending the parent session saves an owned ordinary recording; unrelated standalone recordings are not stopped by a broadcast status change. Pause is offered only for GSR regular recording, because upstream cannot pause a secondary recording inside replay/streaming.

Pause and resume mirror explicit states into the Layer ordinary-recording timeline; replay stays continuous. Timeline sections omit paused intervals during editing/export, while original media remains non-destructive and may contain paused content. Pause is not a privacy-redaction or purge action. A GSR region/window choice does not limit Layer's separate display inventory; crop or remove sources in the editor.

The GSR overlay has one Recordings entry for the single Layer archive/editor. Record/replay/save controls remain in their usual GSR groups; there is no duplicate Layer-only capture menu. Display hotplug is reconciled from GSR inventory every two seconds without clearing the RAM ring. Old source generations remain available while their footage is inside the replay/ordinary-recording window. A failed inventory probe keeps the current producers alive; portal/window/camera selections do not rediscover or reopen consent dialogs on a timer.

Successful Layer saves are silent. The bridge uses application identity instead of private window titles. GSR's own notifications remain controlled by its preferences.

## Portable standalone bindings

Any desktop can bind these commands through its normal shortcut tool:

| Command | Action |
| --- | --- |
| `layer ui` | Open the single archive/editor window |
| `layer gsr <action>` | Send a normal GSR capture/stream/screenshot action through its overlay |
| `layer record` | Toggle ordinary recording |
| `layer replay` | Toggle replay |
| `layer save-1` | Save configured short duration |
| `layer save-5` | Save configured long duration |

Layer's additional shortcut watcher manages owned bindings only on Hyprland. Sway/X11/other desktops can use external bindings; this is separate from GSR's own global shortcut implementation. Layer has no independent GlobalShortcuts portal client. The installer does not rewrite compositor configuration; [the Hyprland example](../examples/hyprland.conf) is optional. Resolve key conflicts through the owning desktop/GSR settings.

## Login startup

The source installer supports a systemd user service and an XDG autostart entry:

```sh
python3 install.py --enable
```

`layer autostart` ensures the capture service is available and falls back to a detached daemon if no usable user service manager exists. It does not open the editor. The desktop session must provide the relevant display/audio environment. `auto_replay` enables standalone replay at service startup; GSR replay autostart can also mirror replay-on. Save actions need an active ring, and a partly filled ring saves only available footage.

The optional GSR service override has Wants/After for `katman.service`. It uses the actual GSR user service selected during installation; the default source-installer name is `gsr-ui.service`. No installation forcibly restarts active GSR capture. Save/stop it before activating a newly built overlay.

## Native packaged overlay

A native package places the patched overlay at `/usr/lib/layer-recorder/overlay/bin/gsr-ui`. To use it with an existing user GSR service, add an override to that real service after saving/stopping capture:

```ini
[Unit]
Wants=katman.service
After=katman.service

[Service]
ExecStart=
ExecStart=/usr/lib/layer-recorder/overlay/bin/gsr-ui launch-daemon
```

Keep the installed `layer` command on PATH. Reload the user service manager and restart the selected GSR service through the normal desktop workflow. Remove the override to return to the distribution overlay. Layer does not replace the system GSR binary.

## Flatpak overlay

Use the bundled overlay rather than a stock GSR UI that lacks the Layer bridge:

```sh
flatpak run io.github.miruas.Layer overlay
```

Host KMS/global helper paths use a system Flatpak installation; a user-only editor installation does not provide those host paths. Desktop authorization and drivers remain host responsibilities. See [Flatpak installation](FLATPAK.md) for permissions and portal alternatives.

## Desktop metadata

GSR owns source inventory on every session. Hyprland, Sway and EWMH/X11 adapters optionally supply geometry and application classes. Native GNOME/KWin Wayland fall back to `Desktop` naming and synthetic placement rather than installing shell extensions or reading window titles. This limits metadata convenience, not GSR capture availability. See [Desktop providers](desktop-providers.md) for exact detection and fallback rules.

## GSR action examples

```sh
layer gsr status
layer gsr toggle-record
layer gsr toggle-pause
layer gsr replay-save-1-min
layer gsr replay-save-10-min
layer gsr toggle-record-region
layer gsr take-screenshot
layer gsr take-screenshot-region
layer gsr take-screenshot-window
layer gsr show-recording-settings
# Uses your existing GSR streaming configuration:
layer gsr start-stream
layer gsr stop-stream
```

The overlay must be running (`layer overlay`). Its RPC is fire-and-forget; CLI JSON reports dispatch and scope only. `status` reports overlay availability, not broadcast completion. The editor shows the independent-track mirror's recording/replay state and does not infer a live-broadcast state from it. Streaming account setup remains in GSR.
