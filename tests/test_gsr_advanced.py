# SPDX-License-Identifier: GPL-3.0-only
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from katman.configuration import validate_config, validate_capture_advanced, validate_capture_target
from katman.hardware import capture_command
from katman.platform_capture import displays
from katman.display_inventory import _selected_items


class AdvancedGsr(unittest.TestCase):
    def plan(self, advanced=None, target='DP-1', **values):
        cfg=validate_config({'capture_advanced':advanced or {},**values})
        return capture_command({'name':target},cfg,{target},{'vendor':'amd','codecs':['h264']})[0]

    def test_video_options_emit_once_and_preserve_segment_contract(self):
        args=self.plan({'cursor':False,'pixel_format':'yuv444','color_range':'full',
            'framerate_mode':'vfr','keyint':0.5,'low_power':True,
            'video_options':{'preset':'fast','qp':20},'plugins':['/tmp/my plugin.so']})
        for flag,value in [('-cursor','no'),('-pixfmt','yuv444'),('-cr','full'),('-keyint','0.5'),
                           ('-low-power','yes'),('-p','/tmp/my plugin.so'),
                           ('-ffmpeg-video-opts','preset=fast;qp=20')]:
            self.assertEqual(args[args.index(flag)+1],value)
            self.assertEqual(args.count(flag),1)
        self.assertEqual(args[args.index('-c')+1],'segment')
        self.assertIn('segment_time=2;',args[args.index('-ffmpeg-opts')+1])
        self.assertNotIn('-r',args)

    def test_quality_modes_and_cpu_codec_restrictions(self):
        args=self.plan({'bitrate_mode':'vbr','encoder':'cpu'},capture_quality='high')
        self.assertEqual(args[args.index('-bm')+1],'vbr')
        self.assertEqual(args[args.index('-encoder')+1],'cpu')
        for value in ({'capture_advanced':{'encoder':'cpu'},'capture_codec':'hevc'},
                      {'capture_advanced':{'bitrate_mode':'cbr'},'capture_quality':'high'},
                      {'capture_advanced':{'bitrate_mode':'vbr'}}):
            with self.subTest(value=value), self.assertRaises(ValueError):validate_config({**value,'gsr_preferences':False})

    def test_unsafe_or_managed_options_rejected(self):
        for value in ({'extra_args':['-o','/tmp/other']},{'keyint':3},{'keyint':float('nan')},
                      {'audio_codec':'flac'},{'audio_bitrate':True},{'cursor':'no'},
                      {'video_options':{'g':'9999'}},{'video_options':{'preset':'x;-o=bad'}},
                      {'audio_options':{'aac_coder':'fast=bad'}},{'plugins':['relative.so']},
                      {'portal_session_token_filepath':'{output}'},{'region':{'width':2}},
                      {'audio_options':{'cutoff':float('inf')}}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_capture_advanced(value)

    def test_portal_options_and_region_use_base_with_modifiers(self):
        args=self.plan({'restore_portal_session':False,'portal_session_token_filepath':'/tmp/token file'},'portal;hflip=true')
        self.assertEqual(args[args.index('-restore-portal-session')+1],'no')
        self.assertEqual(args[args.index('-portal-session-token-filepath')+1],'/tmp/token file')
        args=self.plan({'region':{'width':640,'height':480,'x':-10,'y':0}},'region')
        self.assertEqual(args[args.index('-region')+1],'640x480+-10+0')

    def test_source_modifier_validation_and_inventory(self):
        target='DP-1;width=50%;hflip=true;valign=end'
        self.assertEqual(validate_capture_target(target),target)
        for target in ('DP-1|DP-2','DP-1;output=x','DP-1;hflip=yes','DP-1;x=1;x=2','DP-1;\n'):
            with self.subTest(target=target),self.assertRaises(ValueError):validate_capture_target(target)
        with patch('katman.platform_capture.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='DP-1|1920x1080',stderr='')),patch('katman.platform_capture._enrich_geometry'):
            item=displays('gsr',['DP-1;hflip=true'])[0]
        self.assertEqual(item['name'],'DP-1;hflip=true')
        selected,missing=_selected_items([{'name':'DP-1','width':1920}],['monitor:DP-1;hflip=true'],{})
        self.assertEqual(selected[0]['name'],'monitor:DP-1;hflip=true')
        self.assertEqual(missing,[])

    def test_all_native_codec_names_validate(self):
        for codec in ('vp8','vp9','hevc_hdr_vulkan','av1_10bit_vulkan'):
            self.assertEqual(validate_config({'capture_codec':codec})['capture_codec'],codec)


class AdvancedSchema(unittest.TestCase):
    def test_public_schema_covers_advanced_defaults_and_codecs(self):
        from katman.configuration import DEFAULTS, CAPTURE_CODECS, ADVANCED_ENUMS, ADVANCED_BOOLS
        root=Path(__file__).resolve().parents[1]
        schema=json.loads((root/'examples/settings.schema.json').read_text())
        self.assertEqual(set(schema['properties']),set(DEFAULTS))
        self.assertEqual(schema['properties']['capture_codec']['enum'],list(CAPTURE_CODECS))
        advanced=schema['properties']['capture_advanced']
        self.assertEqual(advanced['default'],{})
        self.assertFalse(advanced['additionalProperties'])
        self.assertEqual(set(advanced['properties']),set(ADVANCED_ENUMS)|set(ADVANCED_BOOLS)|{
            'keyint','audio_bitrate','portal_session_token_filepath','plugins','region','video_options','audio_options'})
        for name,values in ADVANCED_ENUMS.items():self.assertEqual(advanced['properties'][name]['enum'],list(values))
        for name in ADVANCED_BOOLS:self.assertEqual(advanced['properties'][name]['type'],'boolean')
        self.assertEqual(advanced['properties']['keyint']['maximum'],2)
        self.assertFalse(advanced['properties']['region']['additionalProperties'])
        for path in (root/'examples').glob('config.*.json'):
            validate_config(json.loads(path.read_text()))

    def test_jsonschema_when_available(self):
        try:import jsonschema
        except ImportError:self.skipTest('jsonschema is not installed')
        from katman.configuration import DEFAULTS
        root=Path(__file__).resolve().parents[1]
        schema=json.loads((root/'examples/settings.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        for config in [DEFAULTS,*[json.loads(p.read_text()) for p in (root/'examples').glob('config.*.json')]]:
            jsonschema.validate(config,schema)
        for advanced in ({'keyint':3},{'cursor':'no'},{'video_options':{'g':999}}, {'audio_codec':'flac'}):
            with self.subTest(advanced=advanced),self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({'capture_advanced':advanced},schema)
