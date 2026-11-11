# Desktop providers

Layer detects application identity and optional monitor geometry without
reading window titles, installing desktop extensions, or editing compositor
configuration. GSR remains the owner of the capture inventory.

## What each provider supports

| Session | Application identity | Monitor geometry | Layer shortcuts |
| --- | --- | --- | --- |
| Hyprland (`HYPRLAND_INSTANCE_SIGNATURE`) | `hyprctl activewindow -j`, or `hyprctl clients -j` when a PID is supplied. Uses `class`/`initialClass`. | `hyprctl monitors -j`. Uses monitor position, physical size, `transform` and `scale`. | Existing owned `hyprctl` bindings. |
| Sway (`SWAYSOCK`) | `swaymsg -t get_tree`. Uses the focused view's `app_id`, or `window_properties.class`/`instance` for XWayland. PID lookup uses the view node's `pid`. | `swaymsg -t get_outputs`. Uses `rect` position, `current_mode` size, `transform` and `scale`. | Not managed. Use your Sway configuration. |
| X11, including GNOME and KDE Plasma (`DISPLAY`) | EWMH only: `xprop -root _NET_ACTIVE_WINDOW` followed by `xprop -id ID WM_CLASS`. Prefers the WM_CLASS class name and falls back to the instance. | `xrandr --query`. Uses connected-output geometry and the `left`/`right` rotation token. RandR has no portable per-output scale value, so logical size equals the reported rotated size. | Not managed. Use your window manager configuration. |
| GNOME on Wayland | Unsupported. Returns `Desktop`. No shell extension, JavaScript evaluation or D-Bus scraping. | `synthetic`. | `external`. |
| KWin/KDE Plasma on Wayland | Unsupported. Returns `Desktop`. No KWin script, extension or D-Bus scraping. | `synthetic`. | `external`. |
| Headless/unknown | Returns `Desktop`. | `synthetic`. | `external`. |

Provider precedence is Hyprland, Sway, X11, then the truthful desktop fallback.
The X11 adapter is used when `XDG_SESSION_TYPE` is `x11` and `DISPLAY` is set.
When the session type is absent, a display without `WAYLAND_DISPLAY` is detected
as X11. GNOME and KWin use this adapter in their X11 sessions. Their native
Wayland sessions never fall through to the XWayland display: identity remains
`Desktop` and layout remains synthetic. No extensions or shell scripts are installed.

## GSR owns the inventory

`GSR --list-monitors` is the authoritative list of capture outputs. Desktop
adapters never add, remove or rename a GSR output:

- Geometry is applied only to entries whose names exactly match a GSR output
  name.
- Unmatched compositor outputs are ignored.
- A GSR name that the adapter does not know keeps its GSR size and the
  synthetic horizontal placement.
- `portal`, `focused`, numeric and `/dev/video*` targets keep GSR-owned
  placeholder geometry.

The initial fallback is the GSR dimensions laid out left to right with
`layout_provider: synthetic`. A failed, missing or slow adapter leaves that
layout intact. Optional integration therefore never blocks capture.

## Window identity and privacy

Only window classes, application IDs and PIDs are used. Layer never reads
`_NET_WM_NAME`, sway `name`, Hyprland `title` or any other title property, so
private document and browser-tab names cannot become folder names.

The following windows are treated as internal and never become an application
name: Layer/Katman, `layer-recorder`, `layer-studio`, GSR (`gsr`, `gsr-ui`,
`gsr-notify`, `gpu-screen-recorder`), `quickshell` and `dms`.

When an internal window has focus, the last external application is preserved.
The cache is scoped to the active provider: a Hyprland name is not reused in a
Sway or X11 session, and a Sway name is not reused in Hyprland. If no external
name has been recorded for the current provider, the truthful result is
`Desktop`. The internal focus event does not overwrite the saved name.

## Provider report

`desktop_integrations()` in `katman.identity` and `katman.platform_capture`
returns the same report without running external commands:

```json
{
  "identity": "hyprland",
  "geometry": "hyprland",
  "shortcuts": "hyprland",
  "session": "wayland",
  "desktop": "Hyprland",
  "providers": {
    "hyprland": true,
    "sway": false,
    "x11": false
  },
  "unsupported": []
}
```

Possible values:

| Field | Values |
| --- | --- |
| `identity` | `hyprland`, `sway`, `x11`, `desktop` |
| `geometry` | `hyprland`, `sway`, `xrandr`, `synthetic` |
| `shortcuts` | `hyprland`, `external` |
| `session` | detected `XDG_SESSION_TYPE`, or `wayland`/`x11`/`unknown` inference |
| `providers.hyprland`, `providers.sway`, `providers.x11` | whether that live transport is available |
| `unsupported` | detected native Wayland `GNOME` and/or `KWin`; empty for supported X11 sessions |

`unsupported` describes native Wayland identity support only. It does not imply that GSR
capture is unavailable.

## Command bounds

Every probe has a bounded timeout:

- GSR `--list-monitors`: 15 seconds.
- `hyprctl`, `swaymsg`, `xprop` and `xrandr` identity or geometry probes:
  2 seconds each.

Timeouts, missing executables, non-zero exits, malformed output and unavailable
IPC sockets are caught. Identity falls back to the provider-scoped cached name
or `Desktop`; geometry falls back to GSR/synthetic values; capture planning
continues.

## Non-goals

- No GNOME Shell extension and no KWin script.
- No D-Bus or accessibility-tree name scraping.
- No window-title storage.
- No compositor configuration or keybinding changes outside the existing
  Hyprland shortcut integration.
- No host GUI or display server is required to use the fallback paths.
