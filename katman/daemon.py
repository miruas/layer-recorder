import fcntl
import json
import os
import signal
import socketserver
import sys
import threading
import traceback
from pathlib import Path

from .common import RUNTIME, SOCKET, atomic_json, load_project, settings
from .capture import Recorder


def main():
    lock = open(RUNTIME / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    recorder = Recorder()
    SOCKET.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                raw = self.rfile.readline(1048576)
                message = json.loads(raw)
                action = message["action"]
                if action == "status":
                    result = recorder.status()
                elif action == "set-capture-state":
                    result = recorder.set_capture_state(message["kind"], message["enabled"])
                elif action == "toggle-record":
                    result = recorder.toggle_record()
                elif action == "toggle-replay":
                    result = recorder.toggle_replay()
                elif action == "save-replay":
                    result = recorder.save_replay(int(message["seconds"]))
                elif action == "shutdown":
                    recorder.shutdown()
                    result = "stopped"
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    raise ValueError("Unknown action.")
                reply = {"ok": True, "data": result}
            except Exception as e:
                traceback.print_exc()
                reply = {"ok": False, "error": str(e)}
            try:
                self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
            except BrokenPipeError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    server = Server(str(SOCKET), Handler)
    os.chmod(SOCKET, 0o600)
    def stop(sig, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if settings().get("auto_replay"):
        try:
            recorder.toggle_replay()
        except Exception:
            traceback.print_exc()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        recorder.shutdown()
        server.server_close()
        SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
