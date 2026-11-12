# SPDX-License-Identifier: GPL-3.0-only
import unittest
from unittest.mock import patch

from katman import bridge


class BridgeActions(unittest.TestCase):
    @patch('katman.__main__.subprocess.Popen')
    @patch('katman.bridge.request')
    @patch('katman.__main__.ensure_daemon')
    @patch('katman.bridge.settings', return_value={'replay_short': 60, 'replay_long': 300})
    def test_save_durations_and_explicit_states(self, settings, ensure, request, notify):
        request.return_value = 'project.katman'
        bridge.mirror('save-1')
        request.assert_called_with('gsr-action',command='save-1',seconds=60)
        bridge.mirror('save-5')
        request.assert_called_with('gsr-action',command='save-5',seconds=300)
        self.assertEqual(settings.call_count, 2)
        request.return_value = {'recording': False}
        bridge.mirror('record-off')
        request.assert_called_with('gsr-action',command='record-off')
        count=ensure.call_count
        with self.assertRaises(ValueError):
            bridge.mirror('invalid')
        self.assertEqual(ensure.call_count, count)
        notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
