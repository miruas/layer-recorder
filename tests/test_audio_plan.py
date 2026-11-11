# SPDX-License-Identifier: GPL-3.0-only
import subprocess
import unittest
from unittest.mock import patch
from katman.audio_plan import audio_plan


class AudioPlan(unittest.TestCase):
    @patch('katman.audio_plan.shutil.which', return_value='/usr/bin/pw-dump')
    @patch('katman.audio_plan.subprocess.run')
    def test_pipewire_uses_dynamic_roles(self, run, which):
        plan = audio_plan('gsr', {'supports_app_audio': 'yes'})
        self.assertEqual(plan, {'mode': 'pipewire', 'tracks': [], 'warnings': []})
        run.assert_not_called()

    @patch('katman.audio_plan.subprocess.run')
    def test_pulse_retains_all_devices_without_duplicate_aliases(self, run):
        run.return_value = subprocess.CompletedProcess([], 0,
            'default_output|Output\ndefault_input|Input\nalsa.sink.monitor|Speakers\n'
            'alsa.input|Microphone\nusb.input|USB microphone\nalsa.input|Duplicate\nlayer-loop|Internal\n', '')
        plan = audio_plan('gsr', {'supports_app_audio': 'no'})
        self.assertEqual(plan['mode'], 'pulseaudio')
        self.assertEqual([t['meta']['kind'] for t in plan['tracks']], ['system', 'microphone', 'microphone'])
        self.assertEqual(len(plan['tracks']), 3)
        self.assertTrue(plan['warnings'])
        for track in plan['tracks']:
            self.assertIn(track['selector'], track['command'])
            self.assertNotIn('app:', track['selector'])

    @patch('katman.audio_plan.shutil.which', return_value=None)
    @patch('katman.audio_plan.subprocess.run')
    def test_missing_pw_tools_uses_gsr_device_policy(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, 'default_output|Output\ndefault_input|Input\n', '')
        self.assertEqual(len(audio_plan('gsr', {'supports_app_audio': 'yes'})['tracks']), 2)

    @patch('katman.audio_plan.subprocess.run', side_effect=subprocess.TimeoutExpired('gsr', 10))
    def test_absent_audio_does_not_prevent_display_capture(self, run):
        plan = audio_plan('gsr', {'supports_app_audio': 'no'})
        self.assertEqual(plan['mode'], 'none')
        self.assertFalse(plan['tracks'])
        self.assertIn('Only display', plan['warnings'][0])

    @patch('katman.audio_plan.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', ''))
    def test_empty_inventory_is_explicit(self, run):
        self.assertEqual(audio_plan('gsr', {})['mode'], 'none')
