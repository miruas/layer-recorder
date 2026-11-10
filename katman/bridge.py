# SPDX-License-Identifier: GPL-3.0-only
"""Mirror successful GPU Screen Recorder actions without blocking its event loop."""
import json
from .common import request


def mirror(action):
    from .__main__ import ensure_daemon
    states = {"replay-on": ("replay", True), "replay-off": ("replay", False),
              "record-on": ("record", True), "record-off": ("record", False)}
    if action not in {*states, "save-1", "save-5"}:
        raise ValueError("Unknown GPU Screen Recorder integration action.")
    ensure_daemon()
    if action in states:
        kind, enabled = states[action]
        result = request("set-capture-state", kind=kind, enabled=enabled)
    else:
        result = request("save-replay", seconds=60 if action == "save-1" else 300)
    print(json.dumps(result, ensure_ascii=False))
