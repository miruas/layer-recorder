# SPDX-License-Identifier: GPL-3.0-only
import argparse
import json
import os
import subprocess
import sys
import time

from .common import APP, CACHE, CONFIG, SOCKET, request


def ensure_daemon():
    try:
        request("status")
        return
    except (OSError, RuntimeError):
        pass
    managed = False
    if (CONFIG.parent / "systemd/user/katman.service").is_file():
        try:
            subprocess.run(["systemctl", "--user", "start", "katman.service"],
                           check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            managed = True
        except (OSError, subprocess.SubprocessError):
            pass
    if not managed:
        log = open(CACHE / "service.log", "ab")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(APP), env.get("PYTHONPATH", "")]))
        subprocess.Popen([sys.executable, "-m", "katman.daemon"], cwd=APP, env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        log.close()
    for _ in range(100):
        time.sleep(.05)
        try:
            request("status")
            return
        except (OSError, RuntimeError):
            pass
    raise RuntimeError("The capture service could not start. Log: " + str(CACHE / "service.log"))


def main():
    parser = argparse.ArgumentParser(prog="layer",description="Layer multi-source recorder and editor")
    parser.add_argument("command", nargs="?", default="ui",
                        choices=["ui", "download-model", "overlay", "autostart", "status", "record", "replay", "save-1", "save-5", "shutdown", "export", "bridge", "app-name", "doctor", "config", "gsr"])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--output")
    parser.add_argument("--profile",choices=["auto","nvidia","amd","intel"],default="auto")
    args = parser.parse_args()
    try:
        if args.command=="doctor":
            from .common import settings
            from .hardware import doctor
            print(json.dumps(doctor(settings()),indent=2));return
        if args.command=="config":
            from .configuration import profile,load_settings
            from .common import SETTINGS_FILE,atomic_json
            action=args.path or "show"
            if action=="init":
                if SETTINGS_FILE.exists():parser.error("Settings already exist; edit them or choose another LAYER_CONFIG path.")
                atomic_json(SETTINGS_FILE,profile(args.profile));print("Created configuration: "+str(SETTINGS_FILE))
            elif action=="show":print(json.dumps(load_settings(SETTINGS_FILE),indent=2))
            elif action=="validate":load_settings(SETTINGS_FILE);print("Configuration is valid.")
            else:parser.error("config accepts show, init, or validate")
            return
    except (ValueError,OSError) as e:
        parser.error(str(e))
    if args.command == "app-name":
        from .identity import focused_application
        print(focused_application(args.path))
        return
    if args.command == "download-model":
        script = APP / 'tools/download-model.py'
        if not script.is_file():parser.error('The model downloader is missing from this installation.')
        command = [sys.executable, str(script)]
        if args.output:command += ['--output', args.output]
        os.execv(sys.executable, command)
    if args.command == "overlay":
        binary = APP / 'overlay/bin/gsr-ui'
        if not binary.is_file():
            parser.error('The bundled GPU Screen Recorder overlay is missing.')
        try:
            ensure_daemon()
        except (OSError, RuntimeError) as error:
            print('Layer capture service: ' + str(error), file=sys.stderr)
        os.execv(str(binary), [str(binary), 'launch-daemon'])
    if args.command == "ui":
        from .ui import main as ui_main
        ui_main(args.path)
        return
    if args.command == "gsr":
        from .gsr_control import ACTIONS, run as gsr_run
        if not args.path:
            parser.error("gsr requires an action: " + ", ".join(sorted([*ACTIONS, "status"])))
        try:
            result = gsr_run(args.path)
        except ValueError as error:
            parser.error(str(error))
        except (OSError, RuntimeError) as error:
            print("GPU Screen Recorder: " + str(error), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.path.strip() == "status" and result.get("process") != "running":
            sys.exit(1)
        return
    try:
        if args.command == "bridge":
            from .bridge import mirror
            mirror(args.path)
            return
        if args.command == "export":
            from .export import export_project
            if not args.path or not args.output:
                parser.error("export requires a project path and --output")
            print(export_project(args.path, args.output))
            return
        if args.command == "autostart":
            ensure_daemon()
            return
        ensure_daemon()
        mapping = {"record": "toggle-record", "replay": "toggle-replay", "save-1": "save-replay",
                   "save-5": "save-replay", "status": "status", "shutdown": "shutdown"}
        from .common import settings
        extra = {"seconds": settings()["replay_short" if args.command=="save-1" else "replay_long"]} if args.command.startswith("save-") else {}
        result = request(mapping[args.command], **extra)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command not in ("status", "save-1", "save-5") and not (args.command == "record" and not result.get("recording")):
            text = "Action completed."
            if args.command.startswith("save-"):
                text = "Replay project saved."
            elif args.command == "record":
                text = "Recording started." if result.get("recording") else "Recording project saved."
            elif args.command == "replay":
                text = "Replay buffer enabled." if result.get("replay") else "Replay buffer disabled."
            subprocess.Popen(["notify-send", "Layer", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(str(e), file=sys.stderr)
        try:
            subprocess.Popen(["notify-send", "-u", "critical", "Layer", str(e)])
        except OSError:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
