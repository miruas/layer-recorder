# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for the independent, non-interactive display inventory helper."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from katman.configuration import profile
from katman.display_inventory import display_inventory


INFO = {'vendor': 'amd', 'codecs': ['h264']}


def monitor(name, width=1920, height=1080, x=0):
    return dict(name=name, label=name, x=x, y=0, width=width, height=height,
                logical_width=width, logical_height=height,
                layout_provider='synthetic')


def config(targets=None, vendor='amd'):
    cfg = profile(vendor)
    cfg.update(gsr_binary='synthetic-gsr', capture_targets=list(targets or []))
    return cfg


class StaticSources(unittest.TestCase):
    def test_portal_window_and_camera_are_static_and_never_probed(self):
        targets = ['portal', 'focused', '42', '/dev/video7']
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays') as probe:
            result = display_inventory('/synthetic/root', config(targets))
        probe.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertTrue(result['static'])
        self.assertFalse(result['hotplug'])
        self.assertFalse(result['poll'])
        self.assertEqual([track['key'] for track in result['tracks']],
                         ['display:' + target for target in targets])
        portal = result['tracks'][0]
        self.assertEqual(portal['meta']['type'], 'video')
        index = portal['command'].index('-restore-portal-session')
        self.assertEqual(portal['command'][index + 1], 'yes')
        # No producer is started, so constructing the restore-capable command
        # cannot open a portal prompt during a poll.
        self.assertFalse(any('-a' == arg for track in result['tracks']
                             for arg in track['command']))

    def test_explicit_static_source_is_never_rediscovered_on_a_timer(self):
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays',
                   side_effect=AssertionError('discovery must not run')):
            first = display_inventory('/synthetic/root', config(['portal']))
            second = display_inventory('/synthetic/root', config(['portal']))
        self.assertEqual(first['tracks'], second['tracks'])
        self.assertFalse(first['poll'])


class ProbeOutcomes(unittest.TestCase):
    def test_empty_inventory_without_active_hardware_is_success(self):
        empty = RuntimeError('GSR found no capture outputs. Configure '
                             'capture_targets=["portal"] to request an explicit '
                             'portal selection.')
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays', side_effect=empty), \
             patch('katman.display_inventory._no_active_hardware',
                   return_value=True):
            result = display_inventory('/synthetic/root', config())
        self.assertTrue(result['ok'])
        self.assertTrue(result['empty'])
        self.assertEqual(result['reason'], 'no-active-hardware')
        self.assertEqual(result['tracks'], [])
        self.assertEqual(result['errors'], [])

    def test_empty_inventory_with_active_hardware_is_explicit(self):
        empty = RuntimeError('GSR found no capture outputs.')
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays', side_effect=empty), \
             patch('katman.display_inventory._no_active_hardware',
                   return_value=False):
            result = display_inventory('/synthetic/root', config())
        self.assertFalse(result['ok'])
        self.assertFalse(result['empty'])
        self.assertEqual(result['reason'], 'probe-failed')
        self.assertIn('GSR found no capture outputs', result['error'])

    def test_probe_exception_is_transient_and_returns_no_tracks(self):
        timeout = subprocess.TimeoutExpired(['synthetic-gsr'], 15)
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays', side_effect=timeout):
            result = display_inventory('/synthetic/root', config())
        self.assertFalse(result['ok'])
        self.assertTrue(result['transient'])
        self.assertFalse(result['empty'])
        self.assertEqual(result['reason'], 'probe-failed')
        self.assertEqual(result['tracks'], [])
        self.assertFalse(result['poll'])

    def test_capability_exception_is_reported_not_raised(self):
        with patch('katman.display_inventory.gsr_info',
                   side_effect=OSError('synthetic GSR disappeared')):
            result = display_inventory('/synthetic/root', config())
        self.assertFalse(result['ok'])
        self.assertTrue(result['transient'])
        self.assertEqual(result['reason'], 'capability-failed')

    def test_configured_display_disconnect_is_a_successful_empty_plan(self):
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays',
                   return_value=[monitor('HDMI-A-1')]), \
             patch('katman.display_inventory._no_active_hardware',
                   return_value=False):
            result = display_inventory('/synthetic/root', config(['DP-1']))
        self.assertTrue(result['ok'])
        self.assertTrue(result['empty'])
        self.assertEqual(result['reason'], 'configured-displays-absent')
        self.assertEqual(result['missing_targets'], ['DP-1'])
        self.assertTrue(result['warnings'])


class VideoPlan(unittest.TestCase):
    def test_builds_video_only_independent_tracks_from_gsr_inventory(self):
        items = [monitor('DP-1'), monitor('DP-2', 1280, 720, 1920)]
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays',
                   return_value=items) as probe:
            result = display_inventory('/synthetic/root', config())
        probe.assert_called_once_with('synthetic-gsr',include_capture_mode=True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['static'])
        self.assertTrue(result['hotplug'])
        self.assertTrue(result['poll'])
        self.assertFalse(result['empty'])
        self.assertEqual([track['key'] for track in result['tracks']],
                         ['display:DP-1', 'display:DP-2'])
        self.assertEqual([track['meta']['type'] for track in result['tracks']],
                         ['video', 'video'])
        self.assertEqual(result['tracks'][0]['meta']['backend'],
                         'GPU Screen Recorder')
        self.assertIn('-w', result['tracks'][0]['command'])
        self.assertFalse(any('-a' == arg for track in result['tracks']
                             for arg in track['command']))

    def test_configured_target_filter_keeps_only_selected_displays(self):
        items = [monitor('DP-1'), monitor('DP-2', 1280, 720, 1920)]
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays', return_value=items):
            result = display_inventory('/synthetic/root', config(['DP-2']))
        self.assertEqual([track['key'] for track in result['tracks']],
                         ['display:DP-2'])
        self.assertEqual(result['missing_targets'], [])

    def test_one_missing_configured_display_does_not_hide_the_others(self):
        items = [monitor('DP-2', 1280, 720, 1920)]
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays', return_value=items):
            result = display_inventory('/synthetic/root',
                                       config(['DP-1', 'DP-2']))
        self.assertTrue(result['ok'])
        self.assertEqual([track['key'] for track in result['tracks']],
                         ['display:DP-2'])
        self.assertEqual(result['missing_targets'], ['DP-1'])
        self.assertTrue(result['warnings'])

    def test_plan_failure_returns_no_partial_tracks(self):
        info = dict(INFO, vendor='amd')
        with patch('katman.display_inventory.gsr_info', return_value=info), \
             patch('katman.display_inventory.displays',
                   return_value=[monitor('DP-1')]):
            result = display_inventory('/synthetic/root',
                                       config(vendor='nvidia'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'plan-failed')
        self.assertFalse(result['transient'])
        self.assertEqual(result['tracks'], [])
        self.assertIn('does not match', result['error'])

    def test_mixed_static_and_monitor_targets_are_explicit_and_not_polled(self):
        items = [monitor('DP-1')]
        with patch('katman.display_inventory.gsr_info', return_value=INFO), \
             patch('katman.display_inventory.displays',
                   return_value=items) as probe:
            result = display_inventory('/synthetic/root',
                                       config(['DP-1', 'portal']))
        probe.assert_called_once_with('synthetic-gsr',include_capture_mode=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['static'])
        self.assertFalse(result['poll'])
        self.assertEqual([track['key'] for track in result['tracks']],
                         ['display:DP-1', 'display:portal'])


class HardwareDetection(unittest.TestCase):
    def test_connected_and_disconnected_drm_connectors_are_distinguished(self):
        from katman import display_inventory as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connector = root / 'card0-DP-1' / 'status'
            connector.parent.mkdir(parents=True)
            connector.write_text('disconnected\n')
            with patch.object(module, 'DRM_ROOT', root):
                self.assertTrue(module._no_active_hardware())
            connector.write_text('connected\n')
            with patch.object(module, 'DRM_ROOT', root):
                self.assertFalse(module._no_active_hardware())
        with patch.object(module, 'DRM_ROOT', Path('/synthetic/missing')):
            self.assertFalse(module._no_active_hardware())


if __name__ == '__main__':
    unittest.main()

class BackgroundGeometry(unittest.TestCase):
    def test_background_probe_keeps_last_good_placement_when_optional_adapter_fails(self):
        import io,json,copy
        from contextlib import redirect_stdout
        from katman.display_inventory import main
        previous=dict(name='DP-1',x=-1920,y=20,width=1920,height=1080,logical_width=1280,logical_height=720,
                      capture_width=1920,capture_height=1080,layout_provider='sway')
        meta=dict(previous,x=0,y=0,logical_width=1920,logical_height=1080,layout_provider='synthetic')
        request={'config':{'settings':config(),'capabilities':INFO,'previous':[previous]}}
        for changed in (False,True):
            current=dict(meta,capture_width=2560) if changed else dict(meta)
            result={'ok':True,'tracks':[{'meta':current}]}
            output=io.StringIO()
            with patch('sys.stdin',io.StringIO(json.dumps(request)+'\n')),patch('katman.display_inventory.display_inventory',return_value=copy.deepcopy(result)),redirect_stdout(output):main()
            data=json.loads(output.getvalue())['data']['tracks'][0]['meta']
            self.assertEqual(data['layout_provider'],'synthetic' if changed else 'sway')
            self.assertEqual(data['x'],0 if changed else -1920)
