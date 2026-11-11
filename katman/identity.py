# SPDX-License-Identifier: GPL-3.0-only
"""Application identity for recording folders. Window titles are never read."""
import json
import re
import subprocess
from .common import CACHE, atomic_json

ALIASES = {'firefox': 'Firefox', 'firefox-bin': 'Firefox', 'org.mozilla.firefox': 'Firefox',
           'discord': 'Discord', 'com.discordapp.discord': 'Discord', 'vesktop': 'Discord',
           'chromium': 'Chromium', 'chromium-browser': 'Chromium', 'google-chrome': 'Chrome',
           'google-chrome-stable': 'Chrome', 'brave-browser': 'Brave', 'sober': 'Sober',
           'org.vinegarhq.sober': 'Sober', 'genshinimpact.exe': 'Genshin Impact'}
INTERNAL = {'katman', 'layer', 'layer-recorder', 'layer-studio', 'layer studio',
            'gsr', 'gsr-ui', 'gsr ui', 'gsr-notify', 'gpu-screen-recorder',
            'gpu_screen_recorder', 'quickshell', 'dms'}
_INTERNAL_PREFIXES = ('katman', 'layer-recorder', 'layer-studio', 'gsr',
                      'gpu-screen-recorder', 'gpu_screen_recorder')
COMMAND_TIMEOUT = 2


def _text(value):
    return str(value or '').strip()


def _internal(name):
    key = name.lower()
    return key in INTERNAL or key.startswith(_INTERNAL_PREFIXES)


def application_label(client):
    """Return a safe application name from WM_CLASS/app_id data only.

    ``title`` and ``name`` fields are deliberately ignored. A compositor
    window that belongs to Layer, the overlay or GSR yields ``None`` so that
    the caller can preserve the previous external application.
    """
    if not isinstance(client, dict):
        return None
    properties = client.get('window_properties')
    if not isinstance(properties, dict):
        properties = {}
    name = _text(client.get('class') or client.get('initialClass') or
                 client.get('app_id') or properties.get('class') or
                 properties.get('instance'))
    if not name:
        return None
    key = name.lower()
    if key in ALIASES:
        return ALIASES[key]
    short = (name.rsplit('.', 1)[-1]
             if '.' in name and not name.lower().endswith('.exe') else name)
    if _internal(name) or _internal(short):
        return None
    name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', '_', short).strip(' .')[:80]
    return name if name and name not in ('.', '..') else None


def _output(args):
    value = subprocess.check_output(args, timeout=COMMAND_TIMEOUT)
    return value.decode(errors='replace') if isinstance(value, bytes) else str(value)


def _json(args):
    return json.loads(_output(args))


def _same_pid(value, pid):
    try:
        return int(value) == int(pid)
    except (TypeError, ValueError, OverflowError):
        return False


def _hyprland_client(pid=None):
    if pid:
        clients = _json(['hyprctl', 'clients', '-j'])
        if not isinstance(clients, list):
            return {}
        return next((client for client in clients
                     if isinstance(client, dict) and _same_pid(client.get('pid'), pid)), {})
    value = _json(['hyprctl', 'activewindow', '-j'])
    return value if isinstance(value, dict) else {}


def _tree_nodes(node):
    if not isinstance(node, dict):
        return
    yield node
    for key in ('nodes', 'floating_nodes'):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                yield from _tree_nodes(child)


def _sway_client(pid=None):
    tree = _json(['swaymsg', '-t', 'get_tree'])
    nodes = list(_tree_nodes(tree))
    if pid:
        return next((node for node in nodes
                     if _same_pid(node.get('pid'), pid)), {})
    focused = next((node for node in nodes
                    if node.get('focused') and application_label(node)), None)
    if focused:
        return focused
    return next((node for node in nodes if node.get('focused')), {})


_X11_WINDOW = re.compile(r'window id #\s*(0x[0-9a-fA-F]+|\d+)')


def _x11_client(pid=None):
    # EWMH exposes the focused window and WM_CLASS. No title property is read.
    root = _output(['xprop', '-root', '_NET_ACTIVE_WINDOW'])
    match = _X11_WINDOW.search(root)
    if not match:
        return {}
    try:
        window = int(match.group(1), 0)
    except (TypeError, ValueError, OverflowError):
        return {}
    if window == 0:
        return {}
    output = _output(['xprop', '-id', match.group(1), 'WM_CLASS'])
    values = re.findall(r'"((?:\\.|[^"\\])*)"', output)
    if not values:
        return {}
    # WM_CLASS is (instance, class); the class name is the stable identity.
    return {'class': values[-1] if len(values) > 1 else values[0]}


def _cached_application(provider):
    try:
        value = json.loads((CACHE / 'last-application.json').read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get('provider') != provider:
        return None
    name = _text(value.get('application'))
    return application_label({'class': name}) if name else None


def _remember(provider, name):
    try:
        atomic_json(CACHE / 'last-application.json',
                    {'application': name, 'provider': provider})
    except OSError:
        pass


def desktop_integrations():
    """Return the same provider report as :mod:`katman.platform_capture`."""
    from .platform_capture import desktop_integrations as report
    return report()


def focused_application(pid=None, remember=True):
    """Identify the focused external application for the active desktop.

    Hyprland, Sway and EWMH/X11 provide identity. GNOME, KWin and unknown
    sessions truthfully return ``Desktop`` without extensions or title
    scraping. When a Layer/GSR window has focus, the last external name from
    the same provider is retained; names cached by another provider are never
    reused.
    """
    from .platform_capture import desktop_provider
    provider = desktop_provider()
    if provider == 'desktop':
        return 'Desktop'
    try:
        if provider == 'hyprland':
            client = _hyprland_client(pid)
        elif provider == 'sway':
            client = _sway_client(pid)
        else:
            client = _x11_client(pid)
        name = application_label(client)
        if name:
            if remember:
                _remember(provider, name)
            return name
    except Exception:
        pass
    return _cached_application(provider) or 'Desktop'
