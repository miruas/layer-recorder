# SPDX-License-Identifier: GPL-3.0-only
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from katman.gsr_preferences import (
    config_path,
    effective_settings,
    read_gsr_preferences,
    validate_effective_settings,
)


def layer(**values):
    result = {
        "fps": 60,
        "bitrate": 18000,
        "buffer": 300,
        "replay_short": 60,
        "replay_long": 300,
        "capture_codec": "h264",
        "capture_quality": "custom",
        "export_codec": "libx264",
        "export_quality": 20,
        "gsr_sync": True,
        "gsr_preferences": True,
    }
    result.update(values)
    return result


def gsr(**values):
    record_options = {
        "fps": 120,
        "video_bitrate": 50000,
        "codec": "hevc",
        "video_quality": "very_high",
    }
    record_options.update(values.pop("record_options", {}))
    replay = {
        "time": 600,
        "short_save_duration": 30,
        "long_save_duration": 180,
        "record_options": {"fps": 90, "video_bitrate": 40000, "codec": "av1", "video_quality": "ultra"},
    }
    replay.update(values.pop("replay", {}))
    return {"record": {"record_options": record_options}, "replay": replay, **values}


class GsrPreferencesRead(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(read_gsr_preferences(Path(directory) / "missing"))

    def test_explicit_file_is_read_without_side_effects(self):
        value = gsr()
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "config_ui"
            file.write_text(json.dumps(value))
            before = file.read_bytes()
            self.assertEqual(read_gsr_preferences(file), value)
            self.assertEqual(file.read_bytes(), before)

    def test_invalid_json_and_non_object_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "config_ui"
            file.write_text("{not json")
            with self.assertRaises(ValueError):
                read_gsr_preferences(file)
            file.write_text("[]")
            with self.assertRaises(ValueError):
                read_gsr_preferences(file)


class GsrConfigPaths(unittest.TestCase):
    def test_explicit_environment_override_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "explicit"
            with patch.dict(os.environ, {"LAYER_GSR_CONFIG": str(override)}):
                self.assertEqual(config_path(), override)

    def test_native_location_is_preferred_then_flatpak_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            config = root / "config"
            native = config / "gpu-screen-recorder" / "config_ui"
            flatpak = home / ".var" / "app" / "com.dec05eba.gpu_screen_recorder" / "config" / "gpu-screen-recorder" / "config_ui"
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(config)}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("LAYER_GSR_CONFIG", None)
                self.assertEqual(config_path(), native)
                flatpak.parent.mkdir(parents=True)
                flatpak.write_text("{}")
                self.assertEqual(config_path(), flatpak)
                native.parent.mkdir(parents=True)
                native.write_text("{}")
                self.assertEqual(config_path(), native)


class GsrEffectiveSettings(unittest.TestCase):
    def test_none_returns_deep_copy(self):
        original = layer(extra={"keep": [1, 2]})
        result = effective_settings(original, None)
        self.assertEqual(result, original)
        self.assertIsNot(result, original)
        result["extra"]["keep"].append(3)
        self.assertEqual(original["extra"], {"keep": [1, 2]})

    def test_replay_mode_prefers_replay_options(self):
        original = layer()
        result = effective_settings(original, gsr())
        self.assertEqual(result["fps"], 90)
        self.assertEqual(result["bitrate"], 40000)
        self.assertEqual(result["capture_codec"], "av1")
        self.assertEqual(result["capture_quality"], "ultra")
        # Replay durations are shared by both modes.
        self.assertEqual(result["buffer"], 600)
        self.assertEqual(result["replay_short"], 30)
        self.assertEqual(result["replay_long"], 180)
        self.assertEqual(original["fps"], 60)
        self.assertEqual(original["buffer"], 300)

    def test_record_mode_prefers_record_options(self):
        original = layer()
        result = effective_settings(original, gsr(), mode="record")
        self.assertEqual(result["fps"], 120)
        self.assertEqual(result["bitrate"], 50000)
        self.assertEqual(result["capture_codec"], "hevc")
        self.assertEqual(result["capture_quality"], "very_high")
        self.assertEqual(result["buffer"], 600)
        self.assertEqual(original["fps"], 60)

    def test_active_section_falls_back_to_the_other_record_options(self):
        value = {
            "record": {"record_options": {"fps": 75, "codec": "hevc_10bit"}},
            "replay": {"time": 900, "record_options": {"video_quality": "high"}},
        }
        result = effective_settings(layer(), value, mode="replay")
        self.assertEqual(result["fps"], 75)
        self.assertEqual(result["capture_codec"], "hevc_10bit")
        self.assertEqual(result["capture_quality"], "high")

    def test_quality_maps_to_capture_quality_never_export_crf(self):
        result = effective_settings(layer(export_quality=23, export_codec="libx265"), gsr(record_options={"video_quality": "high"}), mode="record")
        self.assertEqual(result["capture_quality"], "high")
        self.assertEqual(result["export_quality"], 23)
        self.assertEqual(result["export_codec"], "libx265")

    def test_custom_quality_keeps_custom_and_gsr_bitrate(self):
        value = gsr(record_options={"codec": "auto", "video_quality": "custom", "video_bitrate": 52000})
        result = effective_settings(layer(), value, mode="record")
        self.assertEqual(result["capture_quality"], "custom")
        self.assertEqual(result["bitrate"], 52000)
        self.assertEqual(result["capture_codec"], "auto")
        self.assertEqual(result["export_quality"], 20)

    def test_auto_and_hdr_codecs_are_preserved_exactly(self):
        for codec in ("auto", "hevc_10bit", "hevc_hdr", "av1_10bit", "av1_hdr"):
            with self.subTest(codec=codec):
                result = effective_settings(layer(), gsr(record_options={"codec": codec}), mode="record")
                self.assertEqual(result["capture_codec"], codec)

    def test_unknown_layer_and_gsr_fields_are_left_alone(self):
        original = layer(custom="layer-value")
        value = gsr(custom_hotkey={"key": 9, "modifiers": 4}, unknown={"nested": True})
        before = copy.deepcopy(value)
        result = effective_settings(original, value)
        self.assertEqual(result["custom"], "layer-value")
        self.assertEqual(value, before)

    def test_layer_can_opt_out_of_gsr_preferences(self):
        original = layer(gsr_preferences=False)
        result = effective_settings(original, gsr())
        self.assertEqual(result, original)
        self.assertIsNot(result, original)

    def test_invalid_gsr_values_are_rejected(self):
        for value in (
            {"record": []},
            {"record": {"record_options": []}},
            gsr(record_options={"fps": True}),
            gsr(record_options={"fps": 0}),
            gsr(record_options={"fps": 241}),
            gsr(record_options={"video_bitrate": 999}),
            gsr(record_options={"codec": "vp9"}),
            gsr(record_options={"video_quality": "lossless"}),
            gsr(replay={"time": 1}),
            gsr(replay={"short_save_duration": "30"}),
            gsr(replay={"long_save_duration": 601}),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_settings(layer(), value, mode="record")

    def test_short_must_not_exceed_long_or_buffer(self):
        for value in (
            gsr(replay={"short_save_duration": 200, "long_save_duration": 180}),
            gsr(replay={"time": 100}),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_settings(layer(), value, mode="record")

    def test_large_gsr_durations_are_supported(self):
        value = gsr(replay={"time": 86400, "short_save_duration": 7200, "long_save_duration": 43200})
        result = effective_settings(layer(), value)
        self.assertEqual(result["buffer"], 86400)
        self.assertEqual(result["replay_short"], 7200)
        self.assertEqual(result["replay_long"], 43200)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            effective_settings(layer(), gsr(), mode="preview")

    def test_open_ended_validation_accepts_partial_settings(self):
        self.assertEqual(validate_effective_settings({"replay_short": 30}), {"replay_short": 30})
        with self.assertRaises(ValueError):
            validate_effective_settings({"replay_short": 301, "replay_long": 300})
        with self.assertRaises(ValueError):
            validate_effective_settings({"capture_codec": "vp9"})
        with self.assertRaises(ValueError):
            validate_effective_settings({"capture_quality": "lossless"})
        with self.assertRaises(ValueError):
            validate_effective_settings({"buffer": 1})


class GsrCommonSettings(unittest.TestCase):
    def _files(self, directory, layer_values=None, gsr_value=None):
        root = Path(directory)
        layer_file = root / "settings.json"
        gsr_file = root / "config_ui"
        layer_file.write_text(json.dumps(layer_values or {}))
        if gsr_value is not None:
            gsr_file.write_text(json.dumps(gsr_value))
        return layer_file, gsr_file

    def test_common_settings_mode_precedence_and_no_export_mutation(self):
        from katman.common import settings as common_settings
        with tempfile.TemporaryDirectory() as directory:
            layer_file, gsr_file = self._files(directory, {"export_quality": 24, "export_codec": "libx265"}, gsr())
            with patch("katman.common.SETTINGS_FILE", layer_file), patch.dict(os.environ, {"LAYER_GSR_CONFIG": str(gsr_file)}):
                replay = common_settings(mode="replay")
                record = common_settings(mode="record")
        self.assertEqual((replay["fps"], replay["capture_codec"]), (90, "av1"))
        self.assertEqual((record["fps"], record["capture_codec"]), (120, "hevc"))
        for result in (replay, record):
            self.assertEqual(result["export_quality"], 24)
            self.assertEqual(result["export_codec"], "libx265")

    def test_common_settings_optout_ignores_gsr(self):
        from katman.common import settings as common_settings
        with tempfile.TemporaryDirectory() as directory:
            layer_file, gsr_file = self._files(directory, {"gsr_preferences": False, "fps": 48}, gsr())
            with patch("katman.common.SETTINGS_FILE", layer_file), patch.dict(os.environ, {"LAYER_GSR_CONFIG": str(gsr_file)}):
                result = common_settings()
        self.assertEqual(result["fps"], 48)
        self.assertEqual(result["capture_codec"], "h264")

    def test_common_settings_uses_explicit_gsr_config_setting(self):
        from katman.common import settings as common_settings
        with tempfile.TemporaryDirectory() as directory:
            layer_file = Path(directory) / "settings.json"
            gsr_file = Path(directory) / "explicit-config"
            layer_file.write_text(json.dumps({"gsr_config": str(gsr_file)}))
            gsr_file.write_text(json.dumps(gsr(record_options={"fps": 144})))
            with patch("katman.common.SETTINGS_FILE", layer_file), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LAYER_GSR_CONFIG", None)
                result = common_settings(mode="record")
        self.assertEqual(result["fps"], 144)


class ShortGsrBuffer(unittest.TestCase):
    def test_legacy_preset_defaults_fit_a_shorter_gsr_buffer(self):
        effective = effective_settings(layer(), {"replay": {"time": 2}})
        self.assertEqual((effective["buffer"], effective["replay_short"], effective["replay_long"]), (2, 2, 2))


if __name__ == "__main__":
    unittest.main()
