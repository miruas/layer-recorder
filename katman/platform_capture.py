# SPDX-License-Identifier: GPL-3.0-only
"""GSR capture inventory; compositor geometry is optional enrichment.

GSR's ``--list-monitors`` output is authoritative. Optional desktop adapters
only update entries whose names match a GSR output. A missing or broken
adapter never prevents capture: the GSR geometry and synthetic layout remain.
"""
import json
import os
import re
import subprocess

GEOMETRY_TIMEOUT = 2
INVENTORY_TIMEOUT = 15


def _environment(name):
    return str(os.environ.get(name) or "").strip()


def hyprland_available():
    return bool(_environment("HYPRLAND_INSTANCE_SIGNATURE"))


def sway_available():
    return bool(_environment("SWAYSOCK"))


def session_type():
    value = _environment("XDG_SESSION_TYPE").lower()
    if value:
        return value
    names = _desktop_names()
    if "hyprland" in names or "sway" in names:
        return "wayland"
    if _environment("WAYLAND_DISPLAY"):
        return "wayland"
    if _environment("DISPLAY"):
        return "x11"
    return "unknown"


def _desktop_names():
    return " ".join(_environment(key) for key in (
        "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")).lower()


def unsupported_desktops():
    """Native Wayland shells without a portable identity/layout adapter.

    Their X11 sessions use EWMH and RandR like every other X11 desktop.
    """
    if session_type() == "x11":
        return []
    names = _desktop_names()
    found = []
    if "gnome" in names:
        found.append("GNOME")
    if "kde" in names or "kwin" in names or "plasma" in names:
        found.append("KWin")
    return found


def desktop_provider():
    """Return the active identity/layout provider.

    Hyprland and Sway are detected from their live IPC environment. X11 is
    used for X11 sessions, including GNOME and KWin. Their native Wayland
    sessions use the desktop fallback without extensions or D-Bus scraping.
    """
    if hyprland_available():
        return "hyprland"
    if sway_available():
        return "sway"
    if session_type() == "x11" and _environment("DISPLAY"):
        return "x11"
    return "desktop"


def geometry_provider():
    return {"hyprland": "hyprland", "sway": "sway", "x11": "xrandr"}.get(
        desktop_provider(), "synthetic")


def desktop_integrations():
    """Describe identity, geometry and shortcut providers without probing."""
    provider = desktop_provider()
    desktop = (_environment("XDG_CURRENT_DESKTOP") or
               _environment("XDG_SESSION_DESKTOP") or
               _environment("DESKTOP_SESSION"))
    return {
        "identity": provider,
        "geometry": geometry_provider(),
        "shortcuts": "hyprland" if provider == "hyprland" else "external",
        "session": session_type(),
        "desktop": desktop,
        "providers": {
            "hyprland": hyprland_available(),
            "sway": sway_available(),
            "x11": provider == "x11",
        },
        "unsupported": unsupported_desktops(),
    }


def _command(args):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=GEOMETRY_TIMEOUT)


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _number(value, default=1.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > 0 else default


def _rotated(output):
    transform = output.get("transform", 0)
    if isinstance(transform, str):
        value = transform.strip().lower()
        return value in {"90", "270", "flipped-90", "flipped-270",
                         "left", "right"}
    return _integer(transform) % 2 == 1


def _hyprland_geometry(names):
    process = _command(["hyprctl", "monitors", "-j"])
    if process.returncode:
        raise ValueError("Hyprland geometry unavailable")
    monitors = json.loads(process.stdout)
    geometry = {}
    for monitor in monitors:
        name = str(monitor.get("name") or "")
        if name not in names or monitor.get("disabled"):
            continue
        width, height = _integer(monitor.get("width")), _integer(monitor.get("height"))
        if width <= 0 or height <= 0:
            continue
        if _rotated(monitor):
            width, height = height, width
        scale = _number(monitor.get("scale"))
        geometry[name] = {
            "label": monitor.get("description") or name,
            "x": _integer(monitor.get("x")),
            "y": _integer(monitor.get("y")),
            "width": width,
            "height": height,
            "logical_width": round(width / scale),
            "logical_height": round(height / scale),
            "layout_provider": "hyprland",
        }
    return geometry


def _sway_geometry(names):
    process = _command(["swaymsg", "-t", "get_outputs"])
    if process.returncode:
        raise ValueError("Sway geometry unavailable")
    outputs = json.loads(process.stdout)
    geometry = {}
    for output in outputs:
        name = str(output.get("name") or "")
        if name not in names or output.get("active") is False or output.get("disabled"):
            continue
        rect = output.get("rect") if isinstance(output.get("rect"), dict) else {}
        mode = output.get("current_mode") if isinstance(output.get("current_mode"), dict) else {}
        width = _integer(mode.get("width") or rect.get("width"))
        height = _integer(mode.get("height") or rect.get("height"))
        if width <= 0 or height <= 0:
            continue
        if _rotated(output):
            width, height = height, width
        scale = _number(output.get("scale"))
        geometry[name] = {
            "x": _integer(rect.get("x")),
            "y": _integer(rect.get("y")),
            "width": width,
            "height": height,
            # Sway reports layout rectangles in logical pixels; its scale
            # applies to current_mode. The division matches Hyprland fields.
            "logical_width": round(width / scale),
            "logical_height": round(height / scale),
            "layout_provider": "sway",
        }
    return geometry


_XRANDR_GEOMETRY = re.compile(
    r"^(?P<name>\S+)\s+connected\s+(?:primary\s+)?"
    r"(?P<width>\d+)x(?P<height>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)\s*(?P<rest>.*)$")


def _xrandr_geometry(names):
    process = _command(["xrandr", "--query"])
    if process.returncode:
        raise ValueError("X11 geometry unavailable")
    geometry = {}
    for line in process.stdout.splitlines():
        match = _XRANDR_GEOMETRY.match(line.strip())
        if not match:
            continue
        name = match.group("name")
        if name not in names:
            continue
        # RandR lists the current mode dimensions plus a rotation token. Title
        # and capability text inside parentheses is never inspected.
        rotation = match.group("rest").split("(", 1)[0].lower()
        width, height = int(match.group("width")), int(match.group("height"))
        if re.search(r"\b(left|right)\b", rotation):
            width, height = height, width
        geometry[name] = {
            "x": int(match.group("x")),
            "y": int(match.group("y")),
            "width": width,
            "height": height,
            "logical_width": width,
            "logical_height": height,
            "layout_provider": "xrandr",
        }
    return geometry


_ADAPTERS = {
    "hyprland": _hyprland_geometry,
    "sway": _sway_geometry,
    "x11": _xrandr_geometry,
}


def _enrich_geometry(items):
    adapter = _ADAPTERS.get(desktop_provider())
    if adapter is None:
        return
    try:
        geometry = adapter({item["name"] for item in items})
    except Exception:
        # An optional adapter must never block a GSR-owned capture plan.
        return
    for item in items:
        update = geometry.get(item["name"])
        if update:
            item.update(update)


def displays(program, targets=None):
    process = subprocess.run([program, "--list-monitors"], capture_output=True,
                             text=True, timeout=INVENTORY_TIMEOUT)
    standalone = lambda target: (target in ("portal", "focused") or
                                 target.isdecimal() or
                                 target.startswith("/dev/video"))
    if process.returncode and (not targets or not all(standalone(target) for target in targets)):
        raise RuntimeError("GSR display discovery failed: " + process.stderr[-1000:])
    result = []
    x = 0
    for line in process.stdout.splitlines():
        match = re.fullmatch(r"([^|]+)\|(\d+)x(\d+)", line.strip())
        if not match:
            continue
        name, width, height = match.groups()
        width, height = int(width), int(height)
        result.append(dict(name=name, label=name, x=x, y=0, width=width,
                           height=height, logical_width=width,
                           logical_height=height, layout_provider="synthetic"))
        x += width
    if result:
        _enrich_geometry(result)
    if targets:
        selected = []
        for target in targets:
            item = next((monitor for monitor in result if monitor["name"] == target), None)
            if item:
                selected.append(item)
            elif target == "portal":
                selected.append(dict(name="portal", label="GSR portal selection",
                                     x=0, y=0, width=0, height=0,
                                     logical_width=0, logical_height=0,
                                     layout_provider="portal-selected"))
            elif standalone(target):
                selected.append(dict(name=target, label="GSR " + target,
                                     x=0, y=0, width=0, height=0,
                                     logical_width=0, logical_height=0,
                                     layout_provider="capture-source"))
            else:
                raise RuntimeError("GSR cannot enumerate the selected source: " +
                                   target + ". Select portal explicitly for a "
                                   "permission-based capture.")
        result = selected
    if not result:
        raise RuntimeError('GSR found no capture outputs. Configure '
                           'capture_targets=["portal"] to request an explicit '
                           'portal selection.')
    return result
