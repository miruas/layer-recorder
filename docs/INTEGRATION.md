# Desktop and GPU Screen Recorder integration

## Standalone shortcuts

The installer does not rewrite your compositor configuration. [examples/hyprland.conf](../examples/hyprland.conf) contains suggested bindings. Include it from your own Hyprland configuration, adapting the path to your checkout. Restart/reload the compositor configuration through its normal workflow.

Suggested actions:

| Shortcut | Action |
| --- | --- |
| Super+Alt+Z | Open Layer |
| Super+Alt+F7 | Save last minute |
| Super+Alt+F8 | Save last five minutes |
| Super+Alt+F9 | Start/stop normal recording |
| Super+Alt+F10 | Enable/disable replay |

Use your compositor's binding syntax if it differs. A key used elsewhere should be reassigned deliberately; Layer does not remove existing bindings.

## Optional Alt+Z menu

Build the pinned GSR UI source plus the provided patch, then install the service override. The added Layer menu can open the library, toggle capture/replay and save either replay length. The original packaged executable remains intact.

The patch launches the portable `layer` command through PATH, never a developer's absolute path. The generated service environment includes the local launcher directory.

## Mirrored GSR actions

GSR's configured normal-record, replay-enable, full-buffer-save and one-minute-save handlers also invoke the Layer bridge. The bridge uses explicit on/off states rather than toggles, so overlay refreshes and duplicate state messages cannot accidentally reverse capture. It runs outside the overlay event loop.

GSR and Layer are separate capture sessions. Mirroring creates the usual GSR recording and an editable Layer project; it uses additional encoder/memory resources. Layer does not reuse GSR's internal buffer. GSR streaming, pause and screenshots have no Layer equivalent. GSR's selected region/window does not restrict Layer's independent display capture.

The patch also bounds optional application-name helper lookups and avoids naming by private window titles. Successful Layer saves are silent. GSR's own notification preferences remain under GSR's control; this installer does not overwrite them.

## Startup

The optional GSR service override has Wants/After for `katman.service`; GSR replay autostart mirrors replay-on. Standalone users can enable `katman.service` and `auto_replay` instead. Save actions work only after replay has started. Before the ring fills, only the available footage can be saved.

To activate a newly built overlay, save/stop existing GSR capture before restarting its service. Neither build nor installation forcibly restarts it.

## Packaged overlay

The ready-to-install package provides `/usr/lib/layer-recorder/overlay/bin/gsr-ui`. After saving/stopping GSR capture, add an override to your existing **user** GSR service (`systemctl --user edit YOUR-GSR.service`):

```ini
[Unit]
Wants=katman.service
After=katman.service

[Service]
ExecStart=
ExecStart=/usr/lib/layer-recorder/overlay/bin/gsr-ui launch-daemon
```

Then reload the user service manager and restart that GSR service. Keep `/usr/bin` on its PATH so the bridge can find `layer`. This does not replace the system overlay binary. Remove the override to return to the distribution overlay. Use your real service name; the package never guesses it or restarts active captures.

Layer Settings → GSR Integration edits shared shortcuts and optionally synchronizes codec/replay length. Backups stay private under the user state directory. The bundled overlay's `reload-config` rebinds shortcuts immediately; capture encoding changes apply on the next recording/replay start.
