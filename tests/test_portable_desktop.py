# SPDX-License-Identifier: GPL-3.0-only
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from katman.identity import application_label, focused_application
from katman.platform_capture import desktop_integrations, displays


def result(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def geometry_factory(monitors, outputs=None, failure=None):
    def run(args, **kwargs):
        if args[1] == '--list-monitors':
            return result(monitors)
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return result(json.dumps(outputs or []))
    return run


def sway_tree(*nodes):
    """Build a minimal Sway get_tree response around focused leaf nodes."""
    return {'type': 'root', 'nodes': [{'type': 'output', 'nodes': [
        {'type': 'workspace', 'nodes': list(nodes)}]}]}


class PortableApplicationIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / 'last-application.json'

    def tearDown(self):
        self.temp.cleanup()

    def environment(self, values):
        return patch.dict(os.environ, values, clear=True)

    def test_hyprland_uses_wm_class_and_never_the_window_title(self):
        client = {'class': 'firefox', 'initialClass': 'Navigator',
                  'title': 'Private bank tab'}
        with self.environment({'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic'}):
            with patch('katman.identity.CACHE', Path(self.temp.name)), \
                 patch('katman.identity.subprocess.check_output',
                       return_value=json.dumps(client).encode()) as check:
                self.assertEqual(focused_application(), 'Firefox')
                check.assert_called_once_with(['hyprctl', 'activewindow', '-j'],
                                              timeout=2)
        self.assertEqual(json.loads(self.cache.read_text()),
                         {'application': 'Firefox', 'provider': 'hyprland'})

    def test_focused_overlay_preserves_the_last_external_application(self):
        self.cache.write_text(json.dumps(
            {'application': 'Firefox', 'provider': 'hyprland'}))
        overlay = {'class': 'layer-recorder', 'title': 'Layer overlay'}
        with self.environment({'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic'}), \
             patch('katman.identity.CACHE', Path(self.temp.name)), \
             patch('katman.identity.subprocess.check_output',
                   return_value=json.dumps(overlay).encode()):
            self.assertEqual(focused_application(), 'Firefox')
        self.assertEqual(json.loads(self.cache.read_text()),
                         {'application': 'Firefox', 'provider': 'hyprland'})

    def test_sway_app_id_and_xwayland_class_are_used_without_titles(self):
        cases = [
            ({'type': 'con', 'focused': True, 'app_id': 'org.mozilla.firefox',
              'name': 'Private tab'}, 'Firefox'),
            ({'type': 'con', 'focused': True, 'app_id': None,
              'window_properties': {'class': 'discord', 'instance': 'discord'},
              'name': 'Private chat'}, 'Discord'),
            ({'type': 'con', 'focused': True, 'app_id': 'layer-studio',
              'name': 'Firefox is not a title source'}, None),
        ]
        for node, expected in cases:
            with self.subTest(expected=expected), \
                 self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
                 patch('katman.identity.CACHE', Path(self.temp.name)), \
                 patch('katman.identity.subprocess.check_output',
                       return_value=json.dumps(sway_tree(node)).encode()) as check:
                value = focused_application(remember=False)
                self.assertEqual(value, expected or 'Desktop')
                check.assert_called_once_with(['swaymsg', '-t', 'get_tree'],
                                              timeout=2)

    def test_sway_pid_selects_the_matching_tree_node(self):
        tree = sway_tree(
            {'type': 'con', 'pid': 7, 'app_id': 'firefox', 'focused': True},
            {'type': 'con', 'pid': 42, 'app_id': 'vesktop'})
        with self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
             patch('katman.identity.CACHE', Path(self.temp.name)), \
             patch('katman.identity.subprocess.check_output',
                   return_value=json.dumps(tree).encode()):
            self.assertEqual(focused_application(pid=42), 'Discord')

    def test_cross_provider_cache_names_are_never_reused(self):
        self.cache.write_text(json.dumps(
            {'application': 'Firefox', 'provider': 'hyprland'}))
        with self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
             patch('katman.identity.CACHE', Path(self.temp.name)), \
             patch('katman.identity.subprocess.check_output',
                   side_effect=OSError('synthetic sway failure')):
            self.assertEqual(focused_application(), 'Desktop')
        self.assertEqual(json.loads(self.cache.read_text()),
                         {'application': 'Firefox', 'provider': 'hyprland'})

    def test_x11_reads_only_ewmh_active_window_and_wm_class(self):
        outputs = [
            '_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2a00007',
            'WM_CLASS(STRING) = "Navigator", "firefox"',
        ]
        with self.environment({'DISPLAY': ':99', 'XDG_SESSION_TYPE': 'x11'}), \
             patch('katman.identity.CACHE', Path(self.temp.name)), \
             patch('katman.identity.subprocess.check_output',
                   side_effect=[value.encode() for value in outputs]) as check:
            self.assertEqual(focused_application(), 'Firefox')
        self.assertEqual([call.args[0] for call in check.call_args_list], [
            ['xprop', '-root', '_NET_ACTIVE_WINDOW'],
            ['xprop', '-id', '0x2a00007', 'WM_CLASS'],
        ])
        flattened = json.dumps([call.args[0] for call in check.call_args_list])
        self.assertNotIn('_NET_WM_NAME', flattened)
        self.assertNotIn('_NET_WM_PID', flattened)
        self.assertTrue(all(call.kwargs == {'timeout': 2}
                            for call in check.call_args_list))

    def test_gnome_and_kwin_wayland_are_truthful_desktop_without_probing(self):
        for desktop in ('GNOME', 'KDE', 'plasma', 'KWin'):
            with self.subTest(desktop=desktop), \
                 self.environment({'XDG_CURRENT_DESKTOP': desktop,
                                   'XDG_SESSION_TYPE': 'wayland',
                                   'WAYLAND_DISPLAY': 'wayland-0',
                                   'DISPLAY': ':99'}), \
                 patch('katman.identity.CACHE', Path(self.temp.name)), \
                 patch('katman.identity.subprocess.check_output') as check:
                self.assertEqual(focused_application(), 'Desktop')
                check.assert_not_called()

    def test_gnome_and_kwin_x11_use_ewmh_without_reading_titles(self):
        for desktop in ('GNOME', 'KDE', 'plasma', 'KWin'):
            with self.subTest(desktop=desktop), \
                 self.environment({'XDG_CURRENT_DESKTOP': desktop,
                                   'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':99'}), \
                 patch('katman.identity.CACHE', Path(self.temp.name)), \
                 patch('katman.identity.subprocess.check_output', side_effect=[
                     b'_NET_ACTIVE_WINDOW(WINDOW): window id # 0x21',
                     b'WM_CLASS(STRING) = "Navigator", "firefox"']) as check:
                self.assertEqual(focused_application(remember=False), 'Firefox')
                self.assertEqual(desktop_integrations()['identity'], 'x11')
                self.assertEqual(desktop_integrations()['geometry'], 'xrandr')
                self.assertEqual(desktop_integrations()['unsupported'], [])
                self.assertEqual(check.call_args.args[0],
                                 ['xprop', '-id', '0x21', 'WM_CLASS'])

    def test_desktop_integrations_report_is_truthful_per_environment(self):
        cases = [
            ({'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic'},
             ('hyprland', 'hyprland', 'hyprland', [])),
            ({'SWAYSOCK': '/run/user/1/sway-ipc.sock'},
             ('sway', 'sway', 'external', [])),
            ({'DISPLAY': ':99', 'XDG_SESSION_TYPE': 'x11',
              'XDG_CURRENT_DESKTOP': 'i3'},
             ('x11', 'xrandr', 'external', [])),
            ({'DISPLAY': ':99', 'XDG_SESSION_TYPE': 'x11',
              'XDG_CURRENT_DESKTOP': 'GNOME'},
             ('x11', 'xrandr', 'external', [])),
            ({'DISPLAY': ':99', 'XDG_SESSION_TYPE': 'wayland',
              'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'GNOME'},
             ('desktop', 'synthetic', 'external', ['GNOME'])),
            ({}, ('desktop', 'synthetic', 'external', [])),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment), \
                 self.environment(environment):
                report = desktop_integrations()
                self.assertEqual((report['identity'], report['geometry'],
                                  report['shortcuts'], report['unsupported']),
                                 expected)
                self.assertEqual(report['providers']['hyprland'],
                                 'HYPRLAND_INSTANCE_SIGNATURE' in environment)
                self.assertEqual(report['providers']['sway'],
                                 'SWAYSOCK' in environment)

    def test_application_label_ignores_titles_and_internal_windows(self):
        self.assertEqual(application_label(
            {'class': 'org.mozilla.firefox', 'name': 'Private tab'}), 'Firefox')
        for internal in ('layer-recorder', 'layer-studio', 'gsr', 'gsr-ui',
                         'gpu-screen-recorder', 'katman',
                         'com.dec05eba.gpu_screen_recorder'):
            self.assertIsNone(application_label(
                {'class': internal, 'title': 'Firefox'}))


class PortableMonitorGeometry(unittest.TestCase):
    def environment(self, values):
        return patch.dict(os.environ, values, clear=True)

    def test_sway_outputs_enrich_matching_names_and_never_invent_outputs(self):
        monitors = 'DP-1|1920x1080\nHDMI-A-1|1920x1080\nGSR-ONLY|800x600\n'
        outputs = [
            {'name': 'DP-1', 'active': True, 'scale': 2.0,
             'transform': 'normal', 'rect': {'x': 0, 'y': 0,
                                             'width': 960, 'height': 540},
             'current_mode': {'width': 1920, 'height': 1080}},
            {'name': 'HDMI-A-1', 'active': True, 'scale': 1.0,
             'transform': '90', 'rect': {'x': 1920, 'y': 0,
                                         'width': 1080, 'height': 1920},
             'current_mode': {'width': 1920, 'height': 1080}},
            {'name': 'NOT-IN-GSR', 'active': True, 'scale': 1.0,
             'transform': 'normal', 'rect': {'x': 0, 'y': 0,
                                             'width': 640, 'height': 480},
             'current_mode': {'width': 640, 'height': 480}},
        ]
        with self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
             patch('katman.platform_capture.subprocess.run',
                   side_effect=geometry_factory(monitors, outputs)) as run:
            items = displays('synthetic-gsr')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0],
                         ['swaymsg', '-t', 'get_outputs'])
        self.assertEqual(run.call_args_list[1].kwargs['timeout'], 2)
        self.assertEqual([item['name'] for item in items],
                         ['DP-1', 'HDMI-A-1', 'GSR-ONLY'])
        self.assertEqual(items[0], dict(
            name='DP-1', label='DP-1', x=0, y=0, width=1920, height=1080,
            logical_width=960, logical_height=540, layout_provider='sway'))
        self.assertEqual(items[1], dict(
            name='HDMI-A-1', label='HDMI-A-1', x=1920, y=0,
            width=1080, height=1920, logical_width=1080, logical_height=1920,
            layout_provider='sway'))
        self.assertEqual(items[2], dict(
            name='GSR-ONLY', label='GSR-ONLY', x=3840, y=0,
            width=800, height=600, logical_width=800, logical_height=600,
            layout_provider='synthetic'))

    def test_broken_or_missing_sway_adapter_never_blocks_gsr_capture(self):
        failures = (FileNotFoundError('swaymsg is not installed'),
                    result('', returncode=1),
                    result('not json'))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                 self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
                 patch('katman.platform_capture.subprocess.run',
                       side_effect=geometry_factory('SYNTH-A|1920x1080\n',
                                                    failure=failure)):
                self.assertEqual(displays('synthetic-gsr'), [dict(
                    name='SYNTH-A', label='SYNTH-A', x=0, y=0,
                    width=1920, height=1080, logical_width=1920,
                    logical_height=1080, layout_provider='synthetic')])

    def test_xrandr_geometry_applies_rotation_and_matches_gsr_names(self):
        monitors = 'eDP-1|1920x1080\nDP-2|2560x1440\n'
        xrandr = (
            'Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767\n'
            'eDP-1 connected primary 1920x1080+0+0 left (normal left inverted '
            'right x axis y axis) 300mm x 200mm\n'
            '   1920x1080     60.00*+\n'
            'DP-2 connected 2560x1440+1920+-100 (normal left inverted right x '
            'axis y axis) 600mm x 340mm\n'
            '   2560x1440     60.00*+\n'
            'HDMI-9 disconnected (normal left inverted right x axis y axis)\n')

        def run(args, **kwargs):
            if args[1] == '--list-monitors':
                return result(monitors)
            return result(xrandr)

        with self.environment({'DISPLAY': ':99', 'XDG_SESSION_TYPE': 'x11',
                               'XDG_CURRENT_DESKTOP': 'i3'}), \
             patch('katman.platform_capture.subprocess.run',
                   side_effect=run) as execute:
            items = displays('synthetic-gsr')
        self.assertEqual(execute.call_args_list[1].args[0], ['xrandr', '--query'])
        self.assertEqual(execute.call_args_list[1].kwargs['timeout'], 2)
        self.assertEqual(items[0], dict(
            name='eDP-1', label='eDP-1', x=0, y=0, width=1080, height=1920,
            logical_width=1080, logical_height=1920,
            layout_provider='xrandr'))
        self.assertEqual(items[1], dict(
            name='DP-2', label='DP-2', x=1920, y=-100, width=2560,
            height=1440, logical_width=2560, logical_height=1440,
            layout_provider='xrandr'))

    def test_gnome_wayland_never_runs_xrandr_and_keeps_synthetic_layout(self):
        with self.environment({'XDG_CURRENT_DESKTOP': 'GNOME',
                               'XDG_SESSION_TYPE': 'wayland',
                               'WAYLAND_DISPLAY': 'wayland-0',
                               'DISPLAY': ':99'}), \
             patch('katman.platform_capture.subprocess.run',
                   return_value=result('SYNTH-A|1280x720\n')) as run:
            items = displays('synthetic-gsr')
        self.assertEqual(items[0]['layout_provider'], 'synthetic')
        run.assert_called_once_with(['synthetic-gsr', '--list-monitors'],
                                    capture_output=True, text=True, timeout=15)

    def test_enriched_geometry_survives_target_selection(self):
        outputs = [{'name': 'DP-1', 'active': True, 'scale': 1.5,
                    'transform': 'normal', 'rect': {'x': 100, 'y': 50,
                                                    'width': 1280, 'height': 720},
                    'current_mode': {'width': 1920, 'height': 1080}}]
        with self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
             patch('katman.platform_capture.subprocess.run',
                   side_effect=geometry_factory('DP-1|1920x1080\nOTHER|800x600\n',
                                                outputs)):
            items = displays('synthetic-gsr', ['DP-1'])
        self.assertEqual(items, [dict(
            name='DP-1', label='DP-1', x=100, y=50, width=1920, height=1080,
            logical_width=1280, logical_height=720, layout_provider='sway')])

    def test_inventory_and_adapter_commands_have_bounded_timeouts(self):
        with self.environment({'SWAYSOCK': '/run/user/1/sway-ipc.sock'}), \
             patch('katman.platform_capture.subprocess.run',
                   side_effect=geometry_factory('SYNTH-A|1920x1080\n',
                                                [])) as run:
            displays('synthetic-gsr')
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list],
                         [15, 2])


if __name__ == '__main__':
    unittest.main()
