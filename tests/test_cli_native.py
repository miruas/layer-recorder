# SPDX-License-Identifier: GPL-3.0-only
"""Native GSR passthrough retains literal upstream argv, without a shell."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class NativeCLI(unittest.TestCase):
    def test_gsr_passthrough_preserves_every_argument_without_starting_layer(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);program=root/'fake-gsr'
            program.write_text('#!'+sys.executable+'\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n');program.chmod(0o700)
            ipc=root/'gsr-cli';ipc.write_text(program.read_text());ipc.chmod(0o700)
            config=root/'settings.json';config.write_text(json.dumps({'gsr_binary':str(program),'gsr_preferences':False}))
            arguments=['-w','screen;x=10;y=20','-ffmpeg-opts','option=value;another=1','-o',str(root/'literal $(touch marker).mp4')]
            env=dict(os.environ,LAYER_CONFIG=str(config),XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),XDG_RUNTIME_DIR=str(root/'runtime'))
            for separator in ([],['--']):
                result=subprocess.run([sys.executable,'-m','katman','gsr-native',*separator,*arguments],env=env,capture_output=True,text=True,check=True)
                self.assertEqual(json.loads(result.stdout),arguments)
            result=subprocess.run([sys.executable,'-m','katman','gsr-ipc','--','-ipc',str(root/'native.sock'),'save-replay','30'],env=env,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout),['-ipc',str(root/'native.sock'),'save-replay','30'])
            self.assertFalse((root/'marker').exists());self.assertFalse((root/'runtime/katman/control.sock').exists())

class CaptureSettingsValidation(unittest.TestCase):
    def test_worker_checks_resolved_gsr_quality_before_saving(self):
        from unittest.mock import patch
        from katman.editor_worker import request
        from katman.configuration import profile
        config=profile('auto');config.update(capture_quality='high',capture_codec='hevc')
        with patch('katman.common.settings',return_value=config):
            result=request({'operation':'capture_settings','capture_advanced':{'bitrate_mode':'qp','cursor':False}})
            self.assertTrue(result['ok'],result)
            self.assertFalse(result['data']['capture_advanced']['cursor'])
            result=request({'operation':'capture_settings','capture_advanced':{'encoder':'cpu'}})
            self.assertFalse(result['ok']);self.assertIn('h264',result['error'])
        config.update(capture_quality='custom',capture_codec='h264')
        with patch('katman.common.settings',return_value=config):
            result=request({'operation':'capture_settings','capture_advanced':{'bitrate_mode':'qp'}})
            self.assertFalse(result['ok']);self.assertIn('preset',result['error'])
