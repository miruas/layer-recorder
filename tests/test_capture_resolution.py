# SPDX-License-Identifier: GPL-3.0-only
"""Capture limits affect producers, independently of export settings."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from katman.configuration import profile, validate_config
from katman.gsr_preferences import effective_settings, read_gsr_preferences
from katman.hardware import capture_command, capture_resolution_limit


class CaptureResolution(unittest.TestCase):
    def test_presets_native_and_custom_validation(self):
        for value in ('native', '720p', '1080p', '1440p', '4k', {'width': 1600, 'height': 1000}):
            self.assertEqual(validate_config({'capture_resolution': value})['capture_resolution'], value)
        for value in (None, True, 1080, '1080', '4K', {}, {'width': 1920},
                      {'width': 1921, 'height': 1080}, {'width': True, 'height': 1080},
                      {'width': 32770, 'height': 1080}, {'width': 1920, 'height': 0},
                      {'width': 1920, 'height': 1080, 'extra': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({'capture_resolution': value})

    def test_selected_gsr_mode_is_independent_and_does_not_mutate(self):
        layer = profile('amd')
        gsr = {'record': {'record_options': {'change_video_resolution': True, 'video_width': 1920, 'video_height': 1080}},
               'replay': {'record_options': {'change_video_resolution': False, 'video_width': 1280, 'video_height': 720}}}
        original = copy.deepcopy(gsr)
        self.assertEqual(effective_settings(layer, gsr, 'record')['capture_resolution'], {'width': 1920, 'height': 1080})
        self.assertEqual(effective_settings(layer, gsr, 'replay')['capture_resolution'], 'native')
        del gsr['replay']
        self.assertEqual(effective_settings(layer, gsr, 'replay')['capture_resolution'], 'native')
        self.assertEqual(original['record'], gsr['record'])
        self.assertEqual(layer['capture_resolution'], 'native')

    def test_malformed_canonical_settings_fail_before_capture(self):
        for options in ({'change_video_resolution': 'true'},
                        {'change_video_resolution': True},
                        {'change_video_resolution': True, 'video_width': 10, 'video_height': 1080},
                        {'change_video_resolution': True, 'video_width': True, 'video_height': 1080}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                effective_settings(profile('auto'), {'record': {'record_options': options}}, 'record')

    def test_legacy_canonical_resolution_and_odd_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'config_ui'
            file.write_text('record.record_options.change_video_resolution true\nrecord.record_options.video_width 1921\nrecord.record_options.video_height 1081\n')
            self.assertEqual(effective_settings(profile('auto'), read_gsr_preferences(file), 'record')['capture_resolution'],
                             {'width': 1920, 'height': 1080})

    def test_limits_fit_aspect_ratio_even_dimensions_and_never_upscale(self):
        for dimensions, expected in (((2560, 1600), (1728, 1080)), ((1080, 1920), (606, 1080)),
                                     ((1280, 720), (1280, 720)), ((3440, 1440), (1920, 802))):
            with self.subTest(dimensions=dimensions):
                self.assertEqual(capture_resolution_limit(dict(zip(('capture_width', 'capture_height'), dimensions)), '1080p'), expected)
        self.assertEqual(capture_resolution_limit({'capture_width': 1920, 'capture_height': 1080}, '4k'), (1920, 1080))
        self.assertIsNone(capture_resolution_limit({}, 'native'))
        self.assertEqual(capture_resolution_limit({'name': 'portal', 'width': 0, 'height': 0}, '1080p'), (1920, 1080))

    def test_each_vendor_and_portal_get_direct_size_without_quality_changes(self):
        for vendor in ('nvidia', 'amd', 'intel'):
            for target in ('DP-test', 'portal'):
                cfg = profile(vendor)
                cfg.update(capture_resolution='1080p', capture_quality='very_high', capture_codec='hevc_10bit', fps=120)
                args, _ = capture_command({'name': target, 'capture_width': 2560, 'capture_height': 1600}, cfg, {target},
                                          {'vendor': vendor, 'codecs': ['hevc_10bit']})
                for key, value in (('-s', '1728x1080'), ('-f', '120'), ('-k', 'hevc_10bit'), ('-q', 'very_high'), ('-encoder', 'gpu')):
                    self.assertEqual(args[args.index(key) + 1], value)
                self.assertNotIn('-s', capture_command({'name': target}, profile(vendor), {target}, {'vendor': vendor, 'codecs': ['h264']})[0])

    def test_json_schema_contains_all_defaults_and_resolution_contract(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / 'examples/settings.schema.json').read_text())
        self.assertEqual(set(profile('auto')), set(schema['properties']))
        self.assertEqual(schema['properties']['capture_resolution']['oneOf'][1]['properties']['width']['multipleOf'], 2)
