"""Application identity for recording folders. Window titles are never used."""
import json
import re
import subprocess
from pathlib import Path
from .common import CACHE, atomic_json

ALIASES = {'firefox': 'Firefox', 'firefox-bin': 'Firefox', 'org.mozilla.firefox': 'Firefox',
           'discord': 'Discord', 'com.discordapp.discord': 'Discord', 'vesktop': 'Discord',
           'chromium': 'Chromium', 'chromium-browser': 'Chromium', 'google-chrome': 'Chrome',
           'google-chrome-stable': 'Chrome', 'brave-browser': 'Brave', 'sober': 'Sober',
           'org.vinegarhq.sober': 'Sober', 'genshinimpact.exe': 'Genshin Impact'}
INTERNAL = {'katman', 'layer', 'gsr-ui', 'gsr ui', 'gsr-notify', 'quickshell', 'dms'}


def application_label(client):
    name = str(client.get('initialClass') or client.get('class') or '').strip()
    if not name or name.lower() in INTERNAL:
        return None
    key = name.lower()
    if key in ALIASES:
        return ALIASES[key]
    name = name.rsplit('.', 1)[-1] if '.' in name and not name.lower().endswith('.exe') else name
    name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', '_', name).strip(' .')[:80]
    return name if name and name not in ('.', '..') else None


def focused_application(pid=None, remember=True):
    cache = CACHE / 'last-application.json'
    try:
        if pid:
            clients = json.loads(subprocess.check_output(['hyprctl', 'clients', '-j'], timeout=2))
            client = next((c for c in clients if c.get('pid') == int(pid)), {})
        else:
            client = json.loads(subprocess.check_output(['hyprctl', 'activewindow', '-j'], timeout=2))
        name = application_label(client)
        if name:
            if remember:
                atomic_json(cache, {'application': name})
            return name
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    try:
        return application_label({'class': json.loads(cache.read_text())['application']}) or 'Desktop'
    except (OSError, ValueError, KeyError, TypeError):
        return 'Desktop'
