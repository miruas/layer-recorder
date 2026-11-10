import threading
import unittest
from unittest.mock import patch

from katman.capture import Recorder
from katman import bridge


class CaptureState(unittest.TestCase):
    def setUp(self):
        self.r = Recorder.__new__(Recorder)
        self.r.lock = threading.RLock()
        self.r.replay = False
        self.r.record_start = None
        self.events = []
        self.r.status = lambda: {'replay': self.r.replay, 'recording': self.r.record_start is not None}
        def replay():
            self.r.replay = not self.r.replay
            self.events.append(('replay', self.r.replay))
            return self.r.status()
        def record():
            self.r.record_start = None if self.r.record_start is not None else 0
            self.events.append(('record', self.r.record_start is not None))
            return self.r.status()
        self.r.toggle_replay = replay
        self.r.toggle_record = record

    def test_repeated_overlay_refresh_does_not_toggle_capture(self):
        for _ in range(3):
            self.r.set_capture_state('replay', True)
            self.r.set_capture_state('record', True)
        self.assertEqual(self.events, [('replay', True), ('record', True)])
        self.r.set_capture_state('record', False)
        self.r.set_capture_state('record', False)
        self.assertTrue(self.r.replay)
        self.assertEqual(self.events[-1], ('record', False))
        self.r.set_capture_state('replay', False)
        self.r.set_capture_state('replay', False)
        self.assertEqual(len(self.events), 4)

    def test_invalid_states_rejected_without_changing_capture(self):
        for kind, enabled in [('stream', True), ('record', 'false')]:
            with self.assertRaises(ValueError):
                self.r.set_capture_state(kind, enabled)
        self.assertEqual(self.events, [])

    @patch('katman.__main__.subprocess.Popen')
    @patch('katman.bridge.request')
    @patch('katman.__main__.ensure_daemon')
    def test_save_durations_and_explicit_states(self, ensure, request, notify):
        request.return_value = 'project.katman'
        bridge.mirror('save-1')
        request.assert_called_with('save-replay', seconds=60)
        bridge.mirror('save-5')
        request.assert_called_with('save-replay', seconds=300)
        request.return_value = {'recording': False}
        bridge.mirror('record-off')
        request.assert_called_with('set-capture-state', kind='record', enabled=False)
        count=ensure.call_count
        with self.assertRaises(ValueError):
            bridge.mirror('invalid')
        self.assertEqual(ensure.call_count, count)
        notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
