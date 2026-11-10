# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from katman.configuration import profile,validate_config
from katman.hardware import capture_command,render_device,devices

class Profiles(unittest.TestCase):
    def test_each_vendor_profile_validates(self):
        for vendor in ('auto','nvidia','amd','intel'):self.assertEqual(validate_config(profile(vendor))['gpu_vendor'],vendor)
    def test_invalid_config_does_not_silently_change_hardware(self):
        for values in ({'gpu_vendor':'other'},{'fps':True},{'fps':0},{'buffer':60},{'render_device':'/tmp/not-a-device'},{'unknown':1},{'auto_replay':'yes'},{'capture_backend':'wf-recorder'}):
            with self.subTest(values=values),self.assertRaises(ValueError):validate_config(values)
    def test_nvidia_amd_intel_gsr_use_native_gpu_without_cpu_fallback(self):
        for vendor in ('nvidia','amd','intel'):
            args,backend=capture_command({'name':'DP-test'},profile(vendor),{'DP-test'},{'vendor':vendor,'codecs':['h264']},Path('/missing'))
            self.assertEqual(args[args.index('-encoder')+1],'gpu');self.assertEqual(args[args.index('-fallback-cpu-encoding')+1],'no');self.assertEqual(backend,'GPU Screen Recorder')
    def test_absent_direct_source_is_rejected_without_wf_recorder_fallback(self):
        with self.assertRaisesRegex(RuntimeError,'automatic wf-recorder fallback was removed'):
            capture_command({'name':'SYNTHETIC-ABSENT'},profile('amd'),set(),{'vendor':'amd','codecs':['h264']})
    def test_portal_capture_opt_in_restores_the_portal_session(self):
        args,backend=capture_command({'name':'portal'},profile('amd'),{'portal'},{'vendor':'amd','codecs':['h264']})
        self.assertEqual(args[args.index('-restore-portal-session')+1],'yes');self.assertEqual(backend,'GPU Screen Recorder')
    def test_explicit_vendor_cannot_select_a_different_device(self):
        with self.assertRaises(RuntimeError):render_device(profile('intel'),[{'vendor':'amd','device':'/dev/dri/renderD129'}])
        cfg=profile('amd');cfg['render_device']='/dev/dri/renderD128'
        with self.assertRaises(RuntimeError):render_device(cfg,[{'vendor':'amd','device':'/dev/dri/renderD129'}])
    def test_sysfs_vendor_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,vendor in enumerate(('0x10de','0x1002','0x8086')):
                p=root/f'renderD{128+index}'/'device';p.mkdir(parents=True);(p/'vendor').write_text(vendor)
            self.assertEqual([d['vendor'] for d in devices(root)],['nvidia','amd','intel'])
