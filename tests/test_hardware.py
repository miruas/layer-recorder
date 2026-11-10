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
        for values in ({'gpu_vendor':'other'},{'fps':True},{'fps':0},{'buffer':60},{'render_device':'/tmp/not-a-device'},{'unknown':1},{'auto_replay':'yes'}):
            with self.subTest(values=values),self.assertRaises(ValueError):validate_config(values)
    def test_nvidia_amd_intel_gsr_use_native_gpu_without_cpu_fallback(self):
        for vendor in ('nvidia','amd','intel'):
            args,backend=capture_command({'name':'DP-test'},profile(vendor),{'DP-test'},{'vendor':vendor,'codecs':['h264']},Path('/missing'))
            self.assertEqual(args[args.index('-encoder')+1],'gpu');self.assertEqual(args[args.index('-fallback-cpu-encoding')+1],'no');self.assertEqual(backend,'GPU Screen Recorder')
    def test_vaapi_wayland_commands_are_vendor_neutral(self):
        with tempfile.NamedTemporaryFile() as helper:
            for vendor in ('amd','intel'):
                cfg=profile(vendor);cfg.update(capture_codec='hevc',wf_no_dmabuf=True)
                args,backend=capture_command({'name':'VIRTUAL'},cfg,set(),{'vendor':vendor,'codecs':['hevc']},Path(helper.name),[{'vendor':vendor,'device':'/dev/dri/renderD129'}])
                self.assertIn('hevc_vaapi',args);self.assertIn('/dev/dri/renderD129',args);self.assertIn('--no-dmabuf',args);self.assertNotIn('preset=p3',args);self.assertIn('VA-API',backend)
    def test_nvidia_wayland_uses_nvenc(self):
        with tempfile.NamedTemporaryFile() as helper:
            args,_=capture_command({'name':'VIRTUAL'},profile('nvidia'),set(),{'vendor':'nvidia'},Path(helper.name),[{'vendor':'nvidia','device':'/dev/dri/renderD128'}]);self.assertIn('h264_nvenc',args);self.assertIn('preset=p3',args)
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
