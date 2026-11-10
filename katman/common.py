# SPDX-License-Identifier: GPL-3.0-only
import json
import os
import socket
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "assets"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "katman"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "katman"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/katman-{os.getuid()}")) / "katman"
for directory in (CONFIG, CACHE, RUNTIME):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
SOCKET = RUNTIME / "control.sock"


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True,
                          text=True, timeout=kwargs.pop("timeout", 15), **kwargs).stdout.strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


SETTINGS_FILE=Path(os.environ.get('LAYER_CONFIG',CONFIG/'settings.json')).expanduser()


def settings():
    from .configuration import load_settings
    return load_settings(SETTINGS_FILE)


def request(action, **data):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(180)
        s.connect(str(SOCKET))
        s.sendall((json.dumps({"action": action, **data}) + "\n").encode())
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            block = s.recv(65536)
            if not block:
                raise RuntimeError("The capture service closed the connection.")
            chunks.extend(block)
        value = json.loads(chunks)
        if not value.get("ok"):
            raise RuntimeError(value.get("error", "The action could not be completed."))
        return value.get("data")


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", path], timeout=30))


def monitors():
    from .hardware import gsr_program
    from .platform_capture import displays
    cfg=settings()
    return displays(gsr_program(cfg),cfg['capture_targets'])


def graph():
    return json.loads(run(["pw-dump"], timeout=5))


def audio_sources(objects):
    groups = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        p = obj.get("info", {}).get("props", {})
        name = p.get("application.name") or p.get("node.name", "")
        if str(p.get("node.name", "")).startswith("katman-") or str(name).lower() in {"layer","katman","layer capture","katman capture","layer studio"}:
            continue
        if p.get("media.class") == "Stream/Output/Audio":
            from .audio_identity import stream_identity
            key,identity=stream_identity(p)
            group=groups.setdefault(key,{**identity,"nodes":[],"active":False})
            group["nodes"].append(obj["id"])
            group["active"] |= obj.get("info",{}).get("state","running") == "running"
        elif p.get("media.class") in ("Audio/Source", "Audio/Source/Virtual"):
            key = "mic:" + str(p.get("node.name"))
            groups[key] = {"name": p.get("node.description") or name, "kind": "microphone", "nodes": [obj["id"]], "active": True}
    return groups


def load_project(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path /= "project.katman"
    p = json.loads(path.read_text())
    if p.get("format") != "katman" or p.get("version") != 1:
        raise ValueError("This project format is not supported.")
    for source in p["sources"]:
        media = (path.parent / source["file"]).resolve()
        if not media.is_relative_to(path.parent.resolve()):
            raise ValueError("Media files must be inside the project folder.")
    return path, p
