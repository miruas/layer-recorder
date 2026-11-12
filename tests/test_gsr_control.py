# SPDX-License-Identifier: GPL-3.0-only
"""GSR overlay control is an argv-validated RPC dispatcher, not a reimplementation."""
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from katman import bridge, gsr_control


FAKE_CLI = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_GSR_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if len(sys.argv) != 2:
    print("expected one command", file=sys.stderr)
    sys.exit(2)
if sys.argv[1] == "status":
    running = os.environ.get("FAKE_GSR_RUNNING") == "1"
    print("running" if running else "not running")
    sys.exit(0 if running else 1)
if os.environ.get("FAKE_GSR_FAIL") == sys.argv[1]:
    print("overlay rejected " + sys.argv[1], file=sys.stderr)
    sys.exit(2)
sys.exit(0)
"""


class FakeOverlay(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / "gsr-ui-cli"
        self.binary.write_text(FAKE_CLI, encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "argv.log"
        self.environment = patch.dict(os.environ, {
            "FAKE_GSR_LOG": str(self.log),
            "FAKE_GSR_RUNNING": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cli = patch("katman.gsr_control.cli", return_value=str(self.binary))
        self.cli.start()
        self.addCleanup(self.cli.stop)

    def test_all_editor_capture_actions_use_canonical_rpc(self):
        actions=("toggle-record","start-record","stop-record","toggle-record-window",
                 "toggle-replay","start-replay","stop-replay","replay-save",
                 "replay-save-1-min","replay-save-10-min","toggle-show","show-recording-settings")
        for action in actions:
            with self.subTest(action=action):
                result=gsr_control.run(action)
                self.assertTrue(result['dispatched'])
                self.assertEqual(result['gsr_command'],action)
                self.assertEqual(self.commands()[-2:],["status",action])

    def commands(self):
        return self.log.read_text(encoding="utf-8").splitlines()


class ActionValidation(FakeOverlay):
    def test_unknown_action_is_rejected_before_any_process_runs(self):
        with self.assertRaisesRegex(ValueError, "Unknown GSR action"):
            gsr_control.run("definitely-not-gsr")
        self.assertFalse(self.log.exists())

    def test_missing_overlay_cli_is_a_truthful_error(self):
        with patch("katman.gsr_control.cli", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "overlay CLI is missing"):
                gsr_control.run("pause")

    def test_not_running_does_not_send_a_command_or_claim_success(self):
        os.environ["FAKE_GSR_RUNNING"] = "0"
        with self.assertRaisesRegex(RuntimeError, "overlay is not running"):
            gsr_control.run("start-stream")
        self.assertEqual(self.commands(), ["status"])

    def test_overlay_rejection_is_an_error(self):
        os.environ["FAKE_GSR_FAIL"] = "toggle-record-region"
        with self.assertRaisesRegex(RuntimeError, "did not accept"):
            gsr_control.run("region")
        self.assertEqual(self.commands(), ["status", "toggle-record-region"])

    def test_status_reports_process_only(self):
        result = gsr_control.status()
        self.assertEqual(result["process"], "running")
        self.assertNotIn("recording", result)
        self.assertIn("process state only", result["detail"])

    def test_status_reports_not_running_without_inventing_recording_state(self):
        os.environ["FAKE_GSR_RUNNING"] = "0"
        result = gsr_control.status()
        self.assertEqual(result["process"], "not running")
        self.assertNotIn("recording", result)


class RealArgvDispatch(FakeOverlay):
    def test_region_uses_overlay_rpc_and_states_archive_scope(self):
        result = gsr_control.run("region")
        self.assertEqual(self.commands(), ["status", "toggle-record-region"])
        self.assertEqual(result["gsr_command"], "toggle-record-region")
        self.assertEqual(result["layer_archive_scope"], "full-screen")
        self.assertEqual(result["crop"], "editor")
        self.assertIn("full-screen", result["note"])
        self.assertIn("editor crop", result["note"])

    def test_screenshot_does_not_claim_a_layer_project(self):
        result = gsr_control.run("screenshot-window")
        self.assertEqual(self.commands(), ["status", "take-screenshot-window"])
        self.assertFalse(result["layer_project"])
        self.assertIn("does not create or save a Layer project", result["note"])

    def test_pause_uses_overlay_toggle_and_notes_replay_continues(self):
        result = gsr_control.run("pause")
        self.assertEqual(self.commands(), ["status", "toggle-pause"])
        self.assertEqual(result["applies_to"], "normal-recording")
        self.assertEqual(result["replay"], "continues")
        self.assertIn("replay continues", result["note"])

    def test_stream_aliases_send_the_overlay_command(self):
        self.assertEqual(gsr_control.run("stream")["gsr_command"], "toggle-stream")
        self.assertEqual(gsr_control.run("streaming")["gsr_command"], "toggle-stream")
        self.assertEqual(gsr_control.run("start-stream")["gsr_command"], "start-stream")
        self.assertEqual(gsr_control.run("stop-stream")["gsr_command"], "stop-stream")
        self.assertEqual(self.commands(), [
            "status", "toggle-stream",
            "status", "toggle-stream",
            "status", "start-stream",
            "status", "stop-stream",
        ])


class LayerGsrCli(FakeOverlay):
    def main(self, argv):
        from katman.__main__ import main
        output, error = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output), redirect_stderr(error):
            main()
        return output.getvalue(), error.getvalue()

    def test_layer_gsr_status_prints_real_json(self):
        output, _error = self.main(["layer", "gsr", "status"])
        self.assertEqual(json.loads(output)["process"], "running")

    def test_layer_gsr_status_absent_process_exits_nonzero(self):
        os.environ["FAKE_GSR_RUNNING"] = "0"
        with self.assertRaises(SystemExit) as raised:
            self.main(["layer", "gsr", "status"])
        self.assertEqual(raised.exception.code, 1)

    def test_layer_gsr_invalid_action_exits_nonzero(self):
        with self.assertRaises(SystemExit) as raised:
            self.main(["layer", "gsr", "bogus"])
        self.assertEqual(raised.exception.code, 2)

    def test_layer_gsr_requires_an_action(self):
        with self.assertRaises(SystemExit) as raised:
            self.main(["layer", "gsr"])
        self.assertEqual(raised.exception.code, 2)


class BridgePause(FakeOverlay):
    @patch("katman.__main__.ensure_daemon")
    @patch("katman.bridge.request", return_value={"paused": True})
    def test_pause_actions_are_idempotent_explicit_states(self, request, _ensure):
        bridge.mirror("pause-on")
        bridge.mirror("pause-on")
        self.assertEqual(request.call_args_list, [
            unittest.mock.call("gsr-action",command="pause-on"),
            unittest.mock.call("gsr-action",command="pause-on"),
        ])
        bridge.mirror("pause-off")
        self.assertEqual(request.call_args_list[-1], unittest.mock.call("gsr-action",command="pause-off"))

    @patch("katman.__main__.ensure_daemon")
    @patch("katman.bridge.request")
    def test_unknown_bridge_action_does_not_start_the_daemon(self, request, ensure):
        with self.assertRaisesRegex(ValueError, "Unknown GPU Screen Recorder"):
            bridge.mirror("pause")
        ensure.assert_not_called()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
