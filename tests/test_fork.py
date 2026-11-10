# SPDX-License-Identifier: GPL-3.0-only
import json,subprocess,tomllib,unittest
from pathlib import Path
from unittest.mock import patch
from katman.configuration import profile
from katman.hardware import gsr_program,gsr_info
ROOT=Path(__file__).resolve().parents[1]
UPSTREAM='1d4946a16139b446e6308f741e9ba99f2ed5b3a7'

class SourceFork(unittest.TestCase):
    def test_gplv3_and_native_provenance_are_explicit(self):
        metadata=tomllib.loads((ROOT/'pyproject.toml').read_text());self.assertEqual(metadata['project']['license'],'GPL-3.0-only')
        source=(ROOT/'upstream/gpu-screen-recorder/src/cli/commands.c').read_text();self.assertIn('fork_name|Layer',source);self.assertIn(UPSTREAM,source)
        self.assertEqual(json.loads((ROOT/'packaging/native-lock.json').read_text())['gpu-screen-recorder']['revision'],UPSTREAM)
    def test_original_history_is_preserved(self):
        if not (ROOT/'.git').exists():self.skipTest('Git ancestry is checked in a repository checkout.')
        subprocess.run(['git','merge-base','--is-ancestor',UPSTREAM,'HEAD'],cwd=ROOT,check=True,capture_output=True)
    def test_default_uses_the_built_fork_with_an_explicit_system_override(self):
        cfg=profile('auto');self.assertEqual(gsr_program(cfg),str(ROOT/'bin/gpu-screen-recorder'))
        cfg['gsr_binary']='gpu-screen-recorder';self.assertEqual(gsr_program(cfg),'gpu-screen-recorder')
    def test_capability_parser_retains_both_fork_and_upstream_identity(self):
        text='section=system_info\ngsr_version|6.1.7\nfork_name|Layer\nfork_version|0.5.1\nupstream_revision|'+UPSTREAM+'\nsection=gpu_info\nvendor|nvidia\nsection=video_codecs\nh264\n'
        with patch('katman.hardware.subprocess.run',return_value=subprocess.CompletedProcess([],0,text,'')):
            result=gsr_info('test-engine')
        self.assertEqual(result['fork_name'],'Layer');self.assertEqual(result['upstream_revision'],UPSTREAM);self.assertEqual(result['vendor'],'nvidia');self.assertEqual(result['codecs'],['h264'])
