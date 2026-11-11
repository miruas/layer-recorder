#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Native GUI regression: slow font/project I/O must not block event handling.

Run inside an X11 or Wayland desktop. Screenshots and logs remain in the
explicit output directory. No capture core or user recording is started.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="layer-startup-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"library": str(root / "recordings")}))
        environment = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), LAYER_CONFIG=str(settings))
        # Preserve the compositor's socket while isolating Layer's own IPC.
        wayland = environment.get("WAYLAND_DISPLAY")
        if wayland and not os.path.isabs(wayland):
            environment["WAYLAND_DISPLAY"] = str(Path(os.environ["XDG_RUNTIME_DIR"]) / wayland)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        font_match = fake_bin / "fc-match"
        font_match.write_text("#!/bin/sh\nexec sleep 3\n")
        font_match.chmod(0o700)
        blocked = root / "blocked.katman"
        os.mkfifo(blocked, mode=0o600)
        cases = [("normal", {}), ("slow-font", {"PATH": f"{fake_bin}:{environment.get('PATH', '')}"}), ("blocked-project", {})]
        for name, overrides in cases:
            screenshot = output / f"{name}.png"
            screenshot.unlink(missing_ok=True)
            command = [str(binary), "--root", str(root), "--qa-screenshot", str(screenshot), "--qa-after", "0.7"]
            if name == "blocked-project":
                command += ["--project", str(blocked)]
            started = time.monotonic()
            try:
                completed = subprocess.run(command, env=environment | overrides, capture_output=True, timeout=12)
                elapsed = time.monotonic() - started
                log = completed.stdout + completed.stderr
                (output / f"{name}.log").write_bytes(log)
                # The intentionally slow font lookup is three seconds. Seeing
                # the first frame sooner proves it is not on the GUI thread.
                passed = completed.returncode == 0 and screenshot.is_file() and b"LAYER_UI_FRAME 1 " in log
                if name == "slow-font":
                    passed = passed and elapsed < 3
                results.append({"case": name, "passed": passed, "elapsed_seconds": round(elapsed, 3), "exit_code": completed.returncode})
            except subprocess.TimeoutExpired as error:
                (output / f"{name}.log").write_bytes((error.stdout or b"") + (error.stderr or b""))
                results.append({"case": name, "passed": False, "error": "Window did not close within 12 seconds"})
    report = {"display": "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11", "results": results}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    for case in results:
        if not case['passed']:
            log = output / (case['case'] + '.log')
            if log.is_file():
                print(case['case'] + ' diagnostic: ' + log.read_text(errors='replace')[-6000:], file=__import__('sys').stderr)
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
