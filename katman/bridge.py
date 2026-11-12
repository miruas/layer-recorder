# SPDX-License-Identifier: GPL-3.0-only
"""Mirror GSR transitions atomically through the independent-source controller."""
import json
from .common import request,settings

ACTIONS = frozenset(("record-on", "record-on-replay", "record-on-stream", "record-off",
                     "replay-on", "replay-off", "stream-on", "stream-off",
                     "pause-on", "pause-off", "save-1", "save-5"))

def mirror(action):
    if action not in ACTIONS:
        raise ValueError("Unknown GPU Screen Recorder integration action.")
    from .__main__ import ensure_daemon
    ensure_daemon()
    extra={}
    if action in ("save-1", "save-5"):
        extra["seconds"]=settings()["replay_short" if action=="save-1" else "replay_long"]
    result=request("gsr-action",command=action,**extra)
    print(json.dumps(result,ensure_ascii=False))
