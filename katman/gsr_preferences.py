# SPDX-License-Identifier: GPL-3.0-only
"""Resolve Layer settings from GSR's public ``config_ui`` preferences.

The GSR UI owns ``config_ui``.  This module only reads a file the caller
explicitly supplies, or a canonical path selected by :func:`config_path`; it
never writes a preference file or changes GSR hotkeys.  Layer uses the public
``record.record_options`` and ``replay`` schema that the GSR UI already saves.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# GSR's public quality presets.  ``custom`` means GSR stores a CBR bitrate in
# ``record_options.video_bitrate`` instead of a preset name.
CAPTURE_QUALITIES = ("custom", "medium", "high", "very_high", "ultra")
CAPTURE_CODECS = (
    "auto",
    "h264",
    "hevc",
    "hevc_10bit",
    "hevc_hdr",
    "av1",
    "av1_10bit",
    "av1_hdr",
)
FLATPAK_APP_ID = "com.dec05eba.gpu_screen_recorder"

_MISSING = object()


def config_path() -> Path:
    """Return the canonical GSR ``config_ui`` path without writing anything.

    ``LAYER_GSR_CONFIG`` is an explicit override.  Otherwise the native GSR UI
    location is preferred, with the original GSR Flatpak location as fallback
    only when that file actually exists.  When neither file exists the native
    location is returned so callers can distinguish a missing default file.
    """
    override = os.environ.get("LAYER_GSR_CONFIG")
    if override:
        return Path(override).expanduser()

    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    native = config_home / "gpu-screen-recorder" / "config_ui"
    if native.is_file():
        return native

    flatpak = (
        Path.home()
        / ".var"
        / "app"
        / FLATPAK_APP_ID
        / "config"
        / "gpu-screen-recorder"
        / "config_ui"
    )
    if flatpak.is_file():
        return flatpak
    return native


# Friendly aliases used by callers and tests.
gsr_config_path = config_path
default_config_path = config_path


def read_gsr_preferences(file: Path | None = None) -> dict | None:
    """Read one explicitly named GSR ``config_ui`` file.

    With no argument, :func:`config_path` chooses the canonical source.
    Returns ``None`` when the file does not exist.  The current GSR JSON schema
    is preferred; GSR's own legacy key/value format is also accepted.  A file
    that cannot be understood raises ``ValueError`` so a caller can report a
    real problem instead of silently replacing GSR settings.
    """
    path = config_path() if file is None else Path(file).expanduser()
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Could not read GSR preferences: " + str(exc)) from exc

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Could not read GSR preferences: " + str(exc)) from exc
        if not isinstance(value, dict):
            raise ValueError("GSR preferences must be a JSON object.")
        return value
    if stripped.startswith("[") or not stripped:
        raise ValueError("GSR preferences must be a JSON object.")
    return _read_legacy_preferences(text)


def _read_legacy_preferences(text: str) -> dict:
    """Translate the relevant GSR legacy key/value lines into the JSON shape."""
    integer_options = {
        "record.record_options.fps",
        "record.record_options.video_bitrate",
        "replay.record_options.fps",
        "replay.record_options.video_bitrate",
        "replay.time",
        *{f"{mode}.record_options.{key}" for mode in ("record", "replay") for key in ("video_width", "video_height")},
        "replay.short_save_duration",
        "replay.long_save_duration",
    }
    boolean_options = {f"{mode}.record_options.change_video_resolution" for mode in ("record", "replay")}
    text_options = {
        "record.record_options.codec",
        "record.record_options.video_quality",
        "replay.record_options.codec",
        "replay.record_options.video_quality",
    }
    result: dict = {}
    recognized = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(" ")
        if not separator or key not in integer_options | text_options | boolean_options:
            continue
        if key in boolean_options:
            if value.strip() not in ("true", "false"):
                raise ValueError(f"Could not read GSR preferences: invalid {key}.")
            parsed = value.strip() == "true"
        elif key in integer_options:
            try:
                parsed: object = int(value.strip(), 10)
            except ValueError as exc:
                raise ValueError(f"Could not read GSR preferences: invalid {key}.") from exc
        else:
            parsed = value.strip()
        node = result
        parts = key.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = node[part] = {}
            elif not isinstance(child, dict):
                raise ValueError(f"Could not read GSR preferences: conflicting {key}.")
            node = child
        node[parts[-1]] = parsed
        recognized += 1
    if not recognized:
        raise ValueError("GSR preferences must be a JSON object.")
    return result


def _mode(mode: str) -> str:
    if mode not in ("replay", "record"):
        raise ValueError("mode must be 'replay' or 'record'.")
    return mode


def _record_options(gsr: dict, section: str):
    section_value = gsr.get(section, _MISSING)
    if section_value is _MISSING:
        return None
    if not isinstance(section_value, dict):
        raise ValueError(f"GSR {section} must be a JSON object.")
    options = section_value.get("record_options", _MISSING)
    if options is _MISSING:
        return None
    if not isinstance(options, dict):
        raise ValueError(f"GSR {section}.record_options must be a JSON object.")
    return options


def _record_option(gsr: dict, mode: str, option: str):
    """Pick the active section's option, falling back to the other section."""
    sections = ("replay", "record") if mode == "replay" else ("record", "replay")
    for section in sections:
        options = _record_options(gsr, section)
        if options is not None and option in options:
            return options[option]
    return _MISSING


def _replay_option(gsr: dict, option: str):
    replay = gsr.get("replay", _MISSING)
    if replay is _MISSING:
        return _MISSING
    if not isinstance(replay, dict):
        raise ValueError("GSR replay must be a JSON object.")
    return replay.get(option, _MISSING)


def _integer(value, key: str, low: int, high: int) -> int:
    if type(value) is not int:
        raise ValueError(f"GSR {key} must be an integer from {low} to {high}.")
    if not low <= value <= high:
        raise ValueError(f"GSR {key} must be an integer from {low} to {high}.")
    return value


def _codec(value, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"GSR {key} must be a video codec name.")
    if value not in CAPTURE_CODECS:
        raise ValueError(
            f"GSR {key} uses {value!r}, which Layer cannot capture. "
            "Choose auto, H.264, HEVC, HEVC 10-bit/HDR, AV1 or AV1 10-bit/HDR in GSR."
        )
    return value


def _uses_gsr(layer: dict) -> bool:
    """GSR is the default source; a Layer preference can opt out."""
    if "gsr_preferences" in layer:
        return bool(layer["gsr_preferences"])
    if "use_gsr_preferences" in layer:
        return bool(layer["use_gsr_preferences"])
    if "gsr_source" in layer:
        return layer["gsr_source"] in (True, "auto", "gsr")
    if "gsr_sync" in layer:
        return bool(layer["gsr_sync"])
    return True


def validate_effective_settings(settings: dict) -> dict:
    """Validate the Layer-facing subset produced by ``effective_settings``.

    The function does not write anything and only checks keys that are
    present, which keeps it useful for partial settings dictionaries.
    """
    if not isinstance(settings, dict):
        raise ValueError("Settings must be a dictionary.")

    if "capture_resolution" in settings:
        from .configuration import validate_capture_resolution
        validate_capture_resolution(settings["capture_resolution"])
    if "fps" in settings:
        _integer(settings["fps"], "fps", 1, 240)
    if "bitrate" in settings:
        _integer(settings["bitrate"], "bitrate", 1000, 200000)
    if "buffer" in settings:
        _integer(settings["buffer"], "buffer", 2, 86400)
    if "replay_short" in settings:
        _integer(settings["replay_short"], "replay_short", 1, 86400)
    if "replay_long" in settings:
        _integer(settings["replay_long"], "replay_long", 1, 86400)
    if "export_quality" in settings:
        _integer(settings["export_quality"], "export_quality", 0, 51)
    if "capture_codec" in settings and settings["capture_codec"] not in CAPTURE_CODECS:
        raise ValueError("capture_codec must be one of: " + ", ".join(CAPTURE_CODECS))
    if "capture_quality" in settings and settings["capture_quality"] not in CAPTURE_QUALITIES:
        raise ValueError("capture_quality must be one of: " + ", ".join(CAPTURE_QUALITIES))

    if {"replay_short", "replay_long"} <= settings.keys() and settings["replay_short"] > settings["replay_long"]:
        raise ValueError("Replay short duration must not exceed the long duration.")
    if {"replay_long", "buffer"} <= settings.keys() and settings["replay_long"] > settings["buffer"]:
        raise ValueError("Replay long duration must not exceed the buffer length.")
    if {"replay_short", "buffer"} <= settings.keys() and settings["replay_short"] > settings["buffer"]:
        raise ValueError("Replay short duration must not exceed the buffer length.")
    return settings


def effective_settings(layer: dict, gsr: dict | None, mode: str = "replay") -> dict:
    """Return a Layer settings copy that follows GSR's public preferences.

    Mapped GSR keys (using the selected mode's ``record_options`` first, then
    the other section as fallback):

    * ``record_options.fps`` -> ``fps``
    * ``record_options.video_bitrate`` -> ``bitrate``
    * ``record_options.codec`` -> ``capture_codec`` (exact, including 10-bit/HDR/auto)
    * ``record_options.video_quality`` -> ``capture_quality``
    * ``replay.time`` -> ``buffer``
    * ``replay.short_save_duration`` -> ``replay_short``
    * ``replay.long_save_duration`` -> ``replay_long``

    Missing GSR keys keep the corresponding Layer value.  Export preferences
    (``export_codec`` and ``export_quality``) are never changed.  The input
    dictionaries are never modified and no configuration file is written.
    """
    if not isinstance(layer, dict):
        raise ValueError("Layer settings must be a dictionary.")
    mode = _mode(mode)
    result = copy.deepcopy(layer)
    if not _uses_gsr(layer):
        return result
    if gsr is None:
        return result
    if not isinstance(gsr, dict):
        raise ValueError("GSR preferences must be a dictionary or None.")

    # Resolution belongs strictly to the selected mode. A missing record
    # setting must never inherit replay's downscale limit, or vice versa.
    options = _record_options(gsr, mode)
    if options is not None and "change_video_resolution" in options:
        enabled = options["change_video_resolution"]
        if type(enabled) is not bool:
            raise ValueError("GSR record_options.change_video_resolution must be true or false.")
        if enabled:
            result["capture_resolution"] = {
                key: _integer(options.get("video_" + key), "record_options.video_" + key, 32, 32768) // 2 * 2
                for key in ("width", "height")
            }
        else:
            result["capture_resolution"] = "native"

    fps = _record_option(gsr, mode, "fps")
    if fps is not _MISSING:
        result["fps"] = _integer(fps, "record_options.fps", 1, 240)

    bitrate = _record_option(gsr, mode, "video_bitrate")
    if bitrate is not _MISSING:
        result["bitrate"] = _integer(bitrate, "record_options.video_bitrate", 1000, 200000)

    codec = _record_option(gsr, mode, "codec")
    if codec is not _MISSING:
        result["capture_codec"] = _codec(codec, "record_options.codec")

    quality = _record_option(gsr, mode, "video_quality")
    if quality is not _MISSING:
        if not isinstance(quality, str):
            raise ValueError("GSR record_options.video_quality must be text.")
        if quality not in CAPTURE_QUALITIES:
            raise ValueError(f"Unsupported GSR video quality: {quality!r}.")
        result["capture_quality"] = quality

    replay_time = _replay_option(gsr, "time")
    if replay_time is not _MISSING:
        result["buffer"] = _integer(replay_time, "replay.time", 2, 86400)

    short_duration = _replay_option(gsr, "short_save_duration")
    if short_duration is not _MISSING:
        result["replay_short"] = _integer(short_duration, "replay.short_save_duration", 1, 86400)

    long_duration = _replay_option(gsr, "long_save_duration")
    if long_duration is not _MISSING:
        result["replay_long"] = _integer(long_duration, "replay.long_save_duration", 1, 86400)

    # Older GSR configs have no Layer save-duration fields. Keep inherited
    # defaults usable when GSR's own buffer is shorter than those defaults.
    if long_duration is _MISSING and "buffer" in result and "replay_long" in result:
        result["replay_long"] = min(result["replay_long"], result["buffer"])
    if short_duration is _MISSING and "buffer" in result and "replay_short" in result:
        result["replay_short"] = min(result["replay_short"], result["buffer"], result.get("replay_long", result["buffer"]))
    return validate_effective_settings(result)
