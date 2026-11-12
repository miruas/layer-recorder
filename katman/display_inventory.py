# SPDX-License-Identifier: GPL-3.0-only
"""Non-interactive GSR display inventory for independent video tracks.

This module is intentionally separate from the audio plan. It never starts a
capture process, so periodic polling cannot open a portal dialog. Explicit
``portal``, ``focused``, numeric-window and ``/dev/video*`` selections are
reported as static sources that must not be rediscovered or restarted on a
timer.

``displays()`` remains the authoritative GSR inventory. Desktop geometry
providers are optional enrichment inside that function; this module neither
requires nor calls Hyprland directly.
"""
import subprocess
from pathlib import Path

from .hardware import capture_command, gsr_info, gsr_program
from .platform_capture import displays

POLL_INTERVAL = 2.0
DRM_ROOT = Path('/sys/class/drm')


def _failure(reason, error, transient, static=False):
    message = str(error)
    return {
        'ok': False,
        'engine': 'rust-gsr',
        'displays': [],
        'tracks': [],
        'empty': False,
        'static': static,
        'hotplug': False,
        'poll': False,
        'reason': reason,
        'missing_targets': [],
        'warnings': [],
        'errors': [message],
        'error': message,
        'transient': bool(transient),
    }


def _standalone(target):
    """A GSR-owned explicit source that is not a discoverable monitor."""
    from .configuration import static_capture_target
    return static_capture_target(target)


def _placeholder(target):
    if target == 'portal':
        return dict(name='portal', label='GSR portal selection',
                    x=0, y=0, width=0, height=0,
                    logical_width=0, logical_height=0,
                    layout_provider='portal-selected')
    return dict(name=target, label='GSR ' + target,
                x=0, y=0, width=0, height=0,
                logical_width=0, logical_height=0,
                layout_provider='capture-source')


def _program(root, cfg):
    configured = str(cfg.get('gsr_binary') or 'auto')
    if configured != 'auto':
        return configured
    if root:
        candidate = Path(root) / 'bin/gpu-screen-recorder'
        if candidate.is_file():
            return str(candidate)
    return gsr_program(cfg)


def _no_active_hardware():
    """True only when DRM connectors exist and none reports ``connected``.

    Missing or unreadable sysfs is treated as unknown, not as proof that the
    machine has no display hardware.
    """
    try:
        connectors = sorted(DRM_ROOT.glob('card*-*/status'))
    except OSError:
        return False
    if not connectors:
        return False
    readable = False
    for connector in connectors:
        try:
            status = connector.read_text(encoding='ascii').strip().lower()
        except OSError:
            continue
        readable = True
        if status == 'connected':
            return False
    return readable


def _empty_inventory_error(error):
    return 'GSR found no capture outputs' in str(error)


def _transient_exception(error):
    if isinstance(error, (subprocess.TimeoutExpired, OSError)):
        return True
    text = str(error).lower()
    return ('timed out' in text or 'timeout' in text or
            'gsr display discovery failed' in text)


def _selected_items(all_items, targets, static_items):
    if not targets:
        return list(all_items), []
    by_name = {item['name']: item for item in all_items}
    selected = []
    missing = []
    for target in targets:
        if target in static_items:
            selected.append(static_items[target])
            continue
        from .configuration import capture_target_base
        item = by_name.get(capture_target_base(target))
        if item is None:
            missing.append(target)
        else:
            selected.append({**item, 'name':target})
    return selected, missing


def _tracks(items, cfg, info, direct):
    tracks = []
    errors = []
    for item in items:
        try:
            command, backend = capture_command(item, cfg, direct, info, None)
        except Exception as error:  # Explicitly fail instead of a partial plan.
            errors.append(item.get('name', 'display') + ': ' + str(error))
            continue
        tracks.append({
            'key': 'display:' + item['name'],
            'meta': {'type': 'video', **item, 'backend': backend},
            'command': command,
        })
    return tracks, errors


def display_inventory(root, cfg, capabilities=None):
    """Return the desired GSR video plan without touching audio or capture.

    The result always contains ``ok``. Expected probe errors are reported in
    ``error``/``errors`` with ``transient`` rather than raised, so a daemon poll
    can keep the current session alive. ``empty`` is a successful empty plan;
    a failed probe is never represented as an empty plan.
    """
    targets = list(cfg.get('capture_targets') or [])
    static_targets = [target for target in targets if _standalone(target)]
    static = bool(static_targets)
    static_items = {target: _placeholder(target) for target in static_targets}
    program = _program(root, cfg)

    try:
        info = capabilities if capabilities is not None else gsr_info(program)
    except Exception as error:
        return _failure('capability-failed', error,
                        _transient_exception(error), static)

    all_items = []
    # If every configured target is a portal/window/camera, do not run GSR
    # discovery at all. This keeps periodic polling completely side-effect free.
    if not (static and len(static_targets) == len(targets)):
        try:
            all_items = displays(program,include_capture_mode=True)
        except Exception as error:
            if _empty_inventory_error(error) and _no_active_hardware():
                all_items = []
            else:
                return _failure('probe-failed', error,
                                _transient_exception(error), static)

    selected, missing = _selected_items(all_items, targets, static_items)
    direct = {item['name'] for item in all_items} | {item['name'] for item in selected} | set(static_items)

    if not selected:
        reason = ('no-active-hardware' if _no_active_hardware()
                  else 'configured-displays-absent' if targets else 'empty')
        result = {
            'ok': True,
            'engine': 'rust-gsr',
            'displays': [],
            'tracks': [],
            'empty': True,
            'static': static,
            'hotplug': not static,
            'poll': not static,
            'reason': reason,
            'missing_targets': missing,
            'warnings': [],
            'errors': [],
            'error': '',
            'transient': False,
        }
        if missing:
            result['warnings'].append(
                'Configured GSR display is not present in the current inventory.')
        return result

    tracks, errors = _tracks(selected, cfg, info, direct)
    if errors:
        error = '; '.join(errors)
        return _failure('plan-failed', error, False, static)

    warnings = []
    if missing:
        warnings.append(
            'Configured GSR display is not present in the current inventory.')
    return {
        'ok': True,
        'engine': 'rust-gsr',
        'displays': selected,
        'tracks': tracks,
        'empty': not tracks,
        'static': static,
        'hotplug': not static,
        'poll': not static,
        'reason': 'ready',
        'missing_targets': missing,
        'warnings': warnings,
        'errors': [],
        'error': '',
        'transient': False,
    }

def main():
    """Dedicated polling process: no shortcut watcher, audio graph or GUI."""
    import json
    import sys
    from .common import APP
    for line in sys.stdin:
        try:
            message=json.loads(line)
            request=message['config']
            data=display_inventory(APP,request['settings'],request.get('capabilities'))
            # Optional compositor geometry is best-effort. Keep the last known
            # placement if its adapter briefly fails and GSR's mode is unchanged.
            old={item.get('name'):item for item in request.get('previous',[])}
            for track in data.get('tracks',[]):
                meta=track['meta'];previous=old.get(meta.get('name'),{})
                raw=('capture_width','capture_height')
                if (meta.get('layout_provider')=='synthetic' and previous.get('layout_provider') not in (None,'synthetic')
                        and all(k in meta and meta[k]==previous.get(k) for k in raw)):
                    for key in ('x','y','width','height','logical_width','logical_height','layout_provider','label'):
                        if key in previous:meta[key]=previous[key]
            reply={'ok':True,'data':data}
        except Exception as error:reply={'ok':False,'error':str(error)}
        print(json.dumps(reply),flush=True)

if __name__=='__main__':main()
