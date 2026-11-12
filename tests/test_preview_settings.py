# SPDX-License-Identifier: GPL-3.0-only
import copy
import json
from pathlib import Path
import tempfile
import unittest

from katman.configuration import DEFAULTS, load_settings, profile, validate_config


class PreviewSettings(unittest.TestCase):
    def test_defaults_follow_source_quality_and_do_not_change_capture(self):
        self.assertEqual(DEFAULTS['preview_resolution'], 'native')
        self.assertEqual(DEFAULTS['preview_fps'], 'source')
        for vendor in ('auto', 'nvidia', 'amd', 'intel'):
            original = profile(vendor)
            result = validate_config({**original, 'preview_resolution': '4k', 'preview_fps': 120})
            for key in ('fps', 'capture_resolution', 'capture_codec', 'capture_quality', 'bitrate', 'export_quality', 'export_codec'):
                self.assertEqual(result[key], original[key])

    def test_all_presets_custom_and_fps_survive_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'settings.json'
            for resolution in ('native', '1080p', '1440p', '4k', {'width': 1600, 'height': 1000}):
                for fps in ('source', 60, 120):
                    values = {'preview_resolution': resolution, 'preview_fps': fps}
                    original = copy.deepcopy(values)
                    result = validate_config(values)
                    self.assertEqual(values, original)
                    file.write_text(json.dumps(result))
                    loaded = load_settings(file)
                    self.assertEqual(loaded['preview_resolution'], resolution)
                    self.assertEqual(loaded['preview_fps'], fps)

    def test_malformed_settings_are_rejected(self):
        for resolution in ('720p', 'Native', 1080, True, None, [], {},
                           {'width': 1920, 'height': 1081}, {'width': 1920.0, 'height': 1080},
                           {'width': 1920, 'height': 1080, 'extra': 1}, {'width': 32770, 'height': 1080}):
            with self.subTest(resolution=resolution), self.assertRaises(ValueError):
                validate_config({'preview_resolution': resolution})
        for fps in (True, False, 60.0, 24, 240, 0, '60', '120', None, {}, []):
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                validate_config({'preview_fps': fps})

    def test_gsr_sync_preserves_explicit_preview_choices(self):
        from katman.gsr_preferences import effective_settings
        layer = validate_config({'preview_resolution': 'native', 'preview_fps': 60})
        gsr = {'record': {'record_options': {'fps': 120, 'change_video_resolution': True, 'video_width': 1920, 'video_height': 1080}}}
        result = effective_settings(layer, gsr, 'record')
        self.assertEqual(result['fps'], 120)
        self.assertEqual(result['preview_fps'], 60)
        self.assertEqual(result['preview_resolution'], 'native')

    def test_public_schema_matches_preview_contract_and_all_defaults(self):
        schema = json.loads((Path(__file__).resolve().parents[1]/'examples/settings.schema.json').read_text())
        self.assertEqual(set(schema['properties']), set(DEFAULTS))
        self.assertEqual(schema['properties']['preview_resolution']['default'], 'native')
        self.assertEqual(schema['properties']['preview_resolution']['oneOf'][0]['enum'], ['native','1080p','1440p','4k'])
        self.assertEqual(schema['properties']['preview_fps']['oneOf'], [{'const':'source'}, {'type':'integer','enum':[60,120]}])
