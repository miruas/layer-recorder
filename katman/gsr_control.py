# SPDX-License-Identifier: GPL-3.0-only
"""Control the bundled GPU Screen Recorder overlay through its normal RPC CLI.

Layer owns its own full-screen archive capture.  Streaming, region capture,
screenshots and pause belong to GPU Screen Recorder; this module only sends the
overlay's documented commands and reports what the CLI actually tells us.
"""
from __future__ import annotations

import shutil
import subprocess

from .common import APP

# CLI action -> (GSR overlay RPC command, truthful note).
# Aliases are deliberate: the short names are convenient, the long names match
# the overlay's own command vocabulary.
ACTIONS = {
    "stream": (
        "toggle-stream",
        "Sent a streaming toggle request to GPU Screen Recorder.",
    ),
    "streaming": (
        "toggle-stream",
        "Sent a streaming toggle request to GPU Screen Recorder.",
    ),
    "toggle-stream": (
        "toggle-stream",
        "Sent a streaming toggle request to GPU Screen Recorder.",
    ),
    "start-stream": (
        "start-stream",
        "Sent a start-streaming request to GPU Screen Recorder.",
    ),
    "stop-stream": (
        "stop-stream",
        "Sent a stop-streaming request to GPU Screen Recorder.",
    ),
    "region": (
        "toggle-record-region",
        "Sent a region-capture toggle request to GPU Screen Recorder. "
        "Layer's archive capture remains full-screen; use the editor crop for a region.",
    ),
    "toggle-record-region": (
        "toggle-record-region",
        "Sent a region-capture toggle request to GPU Screen Recorder. "
        "Layer's archive capture remains full-screen; use the editor crop for a region.",
    ),
    "screenshot": (
        "take-screenshot",
        "Sent a screenshot request to GPU Screen Recorder. GSR writes it to its "
        "own output; this does not create or save a Layer project.",
    ),
    "take-screenshot": (
        "take-screenshot",
        "Sent a screenshot request to GPU Screen Recorder. GSR writes it to its "
        "own output; this does not create or save a Layer project.",
    ),
    "screenshot-region": (
        "take-screenshot-region",
        "Sent a region screenshot request to GPU Screen Recorder. GSR writes it "
        "to its own output; this does not create or save a Layer project.",
    ),
    "take-screenshot-region": (
        "take-screenshot-region",
        "Sent a region screenshot request to GPU Screen Recorder. GSR writes it "
        "to its own output; this does not create or save a Layer project.",
    ),
    "screenshot-window": (
        "take-screenshot-window",
        "Sent a window screenshot request to GPU Screen Recorder. GSR writes it "
        "to its own output; this does not create or save a Layer project.",
    ),
    "take-screenshot-window": (
        "take-screenshot-window",
        "Sent a window screenshot request to GPU Screen Recorder. GSR writes it "
        "to its own output; this does not create or save a Layer project.",
    ),
    "pause": (
        "toggle-pause",
        "Sent a pause toggle request to GPU Screen Recorder. It applies only to "
        "normal GSR recording; replay continues.",
    ),
    "toggle-pause": (
        "toggle-pause",
        "Sent a pause toggle request to GPU Screen Recorder. It applies only to "
        "normal GSR recording; replay continues.",
    ),
}

# Existing GSR capture/settings actions share the same validated RPC path.
for _action in (
    "toggle-record", "start-record", "stop-record", "toggle-record-window",
    "toggle-replay", "start-replay", "stop-replay", "replay-save",
    "replay-save-1-min", "replay-save-10-min", "toggle-show", "show-recording-settings",
):
    ACTIONS[_action] = (_action, "Request sent to GPU Screen Recorder; completion is reported by its capture state.")

_NOT_RUNNING = (
    "GPU Screen Recorder overlay is not running. Start it with `layer overlay`, "
    "then try again."
)

_REGION = {"region", "toggle-record-region"}
_SCREENSHOTS = {
    "screenshot",
    "take-screenshot",
    "screenshot-region",
    "take-screenshot-region",
    "screenshot-window",
    "take-screenshot-window",
}
_PAUSE = {"pause", "toggle-pause"}


def cli():
    """Return the bundled overlay CLI, falling back to one on PATH."""
    local = APP / "overlay/bin/gsr-ui-cli"
    if local.is_file():
        return str(local)
    return shutil.which("gsr-ui-cli")


def _require_cli():
    binary = cli()
    if not binary:
        raise RuntimeError(
            "The GPU Screen Recorder overlay CLI is missing from this installation. "
            "Reinstall Layer with the bundled overlay."
        )
    return binary


def _invoke(binary, command, timeout=10):
    try:
        return subprocess.run(
            [binary, command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "Could not run the GPU Screen Recorder overlay CLI: " + str(error)
        ) from error


def _detail(process):
    return (process.stderr or process.stdout or "").strip()


def _running(binary):
    process = _invoke(binary, "status", timeout=5)
    output = process.stdout.strip()
    if process.returncode == 0 and output == "running":
        return True
    if process.returncode == 1 or output == "not running":
        return False
    raise RuntimeError(
        "Could not query the GPU Screen Recorder overlay: "
        + (_detail(process) or "unexpected status response")
    )


def status():
    """Report the overlay process state.  GSR does not expose recording state."""
    binary = _require_cli()
    running = _running(binary)
    return {
        "process": "running" if running else "not running",
        "detail": "GSR CLI reports overlay process state only.",
    }


def run(action):
    """Dispatch one validated GSR overlay action.

    Raises ValueError for an action outside the allowlist and RuntimeError when
    the overlay is absent or rejects the command.  A successful return means the
    request was written to the overlay socket, not that GSR finished the action.
    """
    key = (action or "").strip()
    if key == "status":
        return status()
    if key not in ACTIONS:
        valid = ", ".join(sorted([*ACTIONS, "status"]))
        raise ValueError("Unknown GSR action. Valid actions: " + valid)

    binary = _require_cli()
    if not _running(binary):
        raise RuntimeError(_NOT_RUNNING)

    command, note = ACTIONS[key]
    process = _invoke(binary, command)
    if process.returncode != 0:
        raise RuntimeError(
            "GPU Screen Recorder did not accept the "
            + command
            + " request: "
            + (_detail(process) or "the overlay returned an error")
        )
    return {
        "action": key,
        "gsr_command": command,
        "dispatched": True,
        "note": note,
        **_scope_state(key),
    }


def _scope_state(key):
    if key in _REGION:
        return {"layer_archive_scope": "full-screen", "crop": "editor"}
    if key in _SCREENSHOTS:
        return {"layer_project": False, "output": "gsr-managed"}
    if key in _PAUSE:
        return {"applies_to": "normal-recording", "replay": "continues"}
    return {}
