# SPDX-License-Identifier: GPL-3.0-only
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from katman.identity import focused_application
from katman.platform_capture import displays


def result(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def run_factory(monitors, hyprland=None, failure=None):
    def run(args, **kwargs):
        if args[1] == '--list-monitors':
            return result(monitors)
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return result(json.dumps(hyprland or []))
    return run


class MonitorInventory(unittest.TestCase):
    def test_parses_gsr_monitors_into_horizontal_synthetic_layout(self):
        monitors = 'SYNTH-LEFT|1920x1080\nnot a monitor line\nSYNTH-RIGHT|1280x720\n'
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
             patch('katman.platform_capture.subprocess.run', return_value=result(monitors)) as run:
            items = displays('synthetic-gsr')
        self.assertEqual(items, [
            dict(name='SYNTH-LEFT', label='SYNTH-LEFT', x=0, y=0, width=1920, height=1080,
                 logical_width=1920, logical_height=1080, layout_provider='synthetic'),
            dict(name='SYNTH-RIGHT', label='SYNTH-RIGHT', x=1920, y=0, width=1280, height=720,
                 logical_width=1280, logical_height=720, layout_provider='synthetic'),
        ])
        run.assert_called_once_with(['synthetic-gsr', '--list-monitors'],
                                    capture_output=True, text=True, timeout=15)

    def test_hyprland_enriches_scale_and_rotation_for_active_monitors(self):
        gsr = 'SYNTH-A|1920x1080\nSYNTH-B|1280x720\n'
        hyprland = [
            {'name': 'SYNTH-A', 'width': 800, 'height': 600, 'x': 900, 'y': 900,
             'scale': 1, 'transform': 0, 'disabled': True, 'description': 'Synthetic Disabled'},
            {'name': 'SYNTH-B', 'width': 1280, 'height': 720, 'x': 1920, 'y': 0,
             'scale': 2, 'transform': 1, 'description': 'Synthetic Display B'},
        ]
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic-signature'}), \
             patch('katman.platform_capture.subprocess.run',
                   side_effect=run_factory(gsr, hyprland)) as run:
            items = displays('synthetic-gsr')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ['synthetic-gsr', '--list-monitors'])
        self.assertEqual(run.call_args_list[1].args[0], ['hyprctl', 'monitors', '-j'])
        self.assertEqual(items[0], dict(
            name='SYNTH-A', label='SYNTH-A', x=0, y=0, width=1920, height=1080,
            logical_width=1920, logical_height=1080, layout_provider='synthetic'))
        self.assertEqual(items[1], dict(
            name='SYNTH-B', label='Synthetic Display B', x=1920, y=0,
            width=720, height=1280, logical_width=360, logical_height=640,
            layout_provider='hyprland'))

    def test_broken_hyprland_metadata_keeps_synthetic_geometry(self):
        gsr = 'SYNTH-A|1920x1080\n'
        failures = (OSError('synthetic hyprctl failure'), result('not-json', returncode=1))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                 patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'synthetic-signature'}), \
                 patch('katman.platform_capture.subprocess.run',
                       side_effect=run_factory(gsr, failure=failure)):
                items = displays('synthetic-gsr')
            self.assertEqual(items, [dict(
                name='SYNTH-A', label='SYNTH-A', x=0, y=0, width=1920, height=1080,
                logical_width=1920, logical_height=1080, layout_provider='synthetic')])

    def test_explicit_portal_target_is_allowed_when_inventory_is_empty(self):
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
             patch('katman.platform_capture.subprocess.run', return_value=result('')) as run:
            items = displays('synthetic-gsr', ['portal'])
        self.assertEqual(items, [dict(
            name='portal', label='GSR portal selection', x=0, y=0, width=0, height=0,
            logical_width=0, logical_height=0, layout_provider='portal-selected')])
        run.assert_called_once_with(['synthetic-gsr', '--list-monitors'],
                                    capture_output=True, text=True, timeout=15)

    def test_unknown_target_is_rejected(self):
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
             patch('katman.platform_capture.subprocess.run',
                   return_value=result('SYNTH-A|1920x1080\n')):
            with self.assertRaisesRegex(
                    RuntimeError,
                    r'GSR cannot enumerate the selected source: SYNTH-UNKNOWN'):
                displays('synthetic-gsr', ['SYNTH-UNKNOWN'])

    def test_empty_inventory_reports_a_clear_error(self):
        for targets in (None, []):
            with self.subTest(targets=targets), \
                 patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
                 patch('katman.platform_capture.subprocess.run', return_value=result('')):
                with self.assertRaisesRegex(
                        RuntimeError,
                        r'GSR found no capture outputs.*capture_targets=\["portal"\]'):
                    displays('synthetic-gsr', targets)

    def test_known_target_selects_only_that_monitor(self):
        gsr = 'SYNTH-A|1920x1080\nSYNTH-B|1280x720\n'
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
             patch('katman.platform_capture.subprocess.run', return_value=result(gsr)):
            items = displays('synthetic-gsr', ['SYNTH-B'])
        self.assertEqual([item['name'] for item in items], ['SYNTH-B'])
        self.assertEqual(items[0]['layout_provider'], 'synthetic')
        self.assertEqual((items[0]['x'], items[0]['width']), (1920, 1280))

    def test_direct_capture_sources_do_not_invent_geometry(self):
        for target in ('focused', '1', '/dev/video42'):
            with self.subTest(target=target), \
                 patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
                 patch('katman.platform_capture.subprocess.run',
                       return_value=result('SYNTH-A|1920x1080\n')):
                items = displays('synthetic-gsr', [target])
            self.assertEqual(items, [dict(
                name=target, label='GSR ' + target, x=0, y=0, width=0, height=0,
                logical_width=0, logical_height=0, layout_provider='capture-source')])


class ApplicationIdentity(unittest.TestCase):
    def test_desktop_fallback_ignores_stale_hyprland_cache_outside_hyprland(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'last-application.json'
            cache.write_text(json.dumps({'application': 'SyntheticStale'}))
            for pid in (None, 4242):
                with self.subTest(pid=pid), \
                     patch('katman.identity.CACHE', Path(directory)), \
                     patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': ''}), \
                     patch('katman.identity.subprocess.check_output') as check_output:
                    self.assertEqual(focused_application(pid=pid), 'Desktop')
                    check_output.assert_not_called()
            self.assertEqual(json.loads(cache.read_text()),
                             {'application': 'SyntheticStale'})


if __name__ == '__main__':
    unittest.main()
