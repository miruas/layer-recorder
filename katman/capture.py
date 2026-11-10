"""Bounded RAM replay with independent, automatically discovered sources.

No audio routing is moved: PipeWire output ports are passively linked to our
capture streams. Display streams are GPU encoded before reaching FFmpeg.
"""
import csv
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .hardware import capture_command, gsr_info
from .buffer_storage import create_ram_buffer, MIN_FREE
from .audio_identity import active_in_window, update_activity, update_presence, has_audio_content
from .common import APP, CACHE, atomic_json, audio_sources, graph, monitors, probe, run, settings


class Track:
    def __init__(self, root, origin, key, meta, command, fps):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.meta = meta
        self.path = root / self.id
        self.path.mkdir()
        self.name = "katman-" + self.id
        self.log = open(self.path / "capture.log", "ab", buffering=0)
        self.offset = time.monotonic() - origin
        self.origin = origin
        self.links = set()
        self.closed = False
        self.error_reported = False
        self.csv_offset = 0
        self.csv_pending = ""
        self.rows = []
        self.activity = [] if meta["type"] == "audio" else None
        audio = meta["type"] == "audio"
        env = os.environ.copy()
        env["KATMAN_FIRST_FRAME_TS"] = str(self.path / "%08d.mkv.ts")
        env["KATMAN_SEGMENT_LIST"] = str(self.path / "segments.csv")
        replacements = {"{node}": self.name, "{output}": str(self.path / "%08d.mkv"),
                        "{list}": str(self.path / "segments.csv")}
        argv = []
        for arg in command:
            value = str(arg)
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
            argv.append(value)
        self.producer = subprocess.Popen(argv, stdout=subprocess.PIPE if audio else subprocess.DEVNULL,
                                         stderr=self.log, env=env)
        if not audio:
            self.muxer = None
            return
        input_args = ["-f", "s16le", "-ar", "48000", "-ac", "2"] if audio else []
        encode = ["-c:a", "flac"] if audio else ["-c:v", "copy", "-an"]
        try:
            self.muxer = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-probesize", "32768", "-analyzeduration", "0", *input_args,
                "-i", "pipe:0", *encode, "-f", "segment", "-segment_time", "2",
                "-segment_list", str(self.path / "segments.csv"), "-segment_list_type", "csv",
                "-reset_timestamps", "1", "-segment_format", "matroska",
                "-segment_format_options", "cluster_time_limit=250:flush_packets=1",
                str(self.path / "%08d.mkv")], stdin=self.producer.stdout, stdout=subprocess.DEVNULL,
                stderr=self.log)
        except BaseException:
            self.producer.terminate()
            self.producer.wait(timeout=10)
            raise
        self.producer.stdout.close()

    def alive(self):
        return self.producer.poll() is None and (self.muxer is None or self.muxer.poll() is None)

    def stop(self):
        if self.closed:
            return
        if self.producer.poll() is None:
            self.producer.send_signal(signal.SIGINT)
        try:
            self.producer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.producer.kill()
            self.producer.wait()
        if self.muxer:
            try:
                self.muxer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.muxer.terminate()
                self.muxer.wait(timeout=5)
        self.closed = True
        self.log.close()

    def segments(self, include_active=False):
        timestamp = self.path / "%08d.mkv.ts"
        if timestamp.exists():
            try:
                self.offset = int(timestamp.read_text().splitlines()[1].split()[0])/1e6 - self.origin
            except (ValueError, IndexError):
                pass
        try:
            with (self.path / "segments.csv").open() as listing:
                listing.seek(self.csv_offset)
                appended = self.csv_pending + listing.read()
                self.csv_offset = listing.tell()
            lines = appended.splitlines(keepends=True)
            self.csv_pending = ""
            if lines and not lines[-1].endswith("\n"):
                self.csv_pending = lines.pop()
            for row in csv.reader(lines):
                if len(row) == 3:
                    file = self.path / Path(row[0]).name
                    self.rows.append((file, float(row[1]), float(row[2])))
        except (OSError, ValueError):
            pass
        rows = list(self.rows)
        if include_active:
            known = {x[0].name for x in rows}
            extras = [f for f in sorted(self.path.glob("*.mkv")) if f.name not in known]
            for f in extras:
                # Segment PTS begins at 0. Closed rows provide the continuous clock.
                start = rows[-1][2] if rows else int(f.stem) * 2
                rows.append((f, start, float("inf")))
        return rows

    def prune(self, cutoff, archive_before=None, archive_root=None):
        kept=[]
        for file, start, end in self.segments():
            if self.offset + end < cutoff:
                file.unlink(missing_ok=True)
                continue
            if archive_before is not None and self.offset + end < archive_before and file.parent==self.path:
                # Only closed segments are moved. The RAM ring remains bounded
                # while an ordinary recording retains older footage on disk.
                target=archive_root/self.id/file.name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(file,target);file.unlink();file=target
            kept.append((file,start,end))
        self.rows=kept


class Recorder:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.replay = False
        self.record_start = None
        self.origin = 0
        self.tracks = []
        self.errors = []
        self.last_project = None
        self.root = None
        self.archive_root = None
        self.stopping = False
        threading.Thread(target=self._watch, daemon=True).start()

    def status(self):
        with self.lock:
            elapsed = time.monotonic() - self.origin if self.running else 0
            return {"running": self.running, "replay": self.replay,
                    "buffer_storage": "ram", "buffer_directory": str(self.root) if self.root else None,
                    "recording": self.record_start is not None,
                    "buffer_seconds": min(300, max(0, elapsed)),
                    "record_seconds": max(0, elapsed - self.record_start) if self.record_start is not None else 0,
                    "sources": [{"id": t.id, **t.meta, "active": t.alive()} for t in self.tracks],
                    "errors": self.errors[-10:], "last_project": self.last_project,
                    "cache_bytes": sum(f.stat().st_size for t in self.tracks for f in t.path.glob("*.mkv") if f.exists())}

    def _start(self):
        if self.running:
            return
        self.errors = []
        self.tracks = []
        cfg = settings()
        self.origin = time.monotonic()
        display_list = monitors()
        if not display_list:
            raise RuntimeError("No displays are available to record.")
        direct = {line.split("|")[0] for line in run([cfg["gsr_binary"], "--list-monitors"]).splitlines()}
        info = gsr_info(cfg["gsr_binary"])
        self.root = create_ram_buffer()
        self.archive_root = CACHE / ("recording-" + self.root.name)
        try:
            for m in display_list:
                meta = {"type": "video", **m}
                cmd,backend=capture_command(m,cfg,direct,info,APP/'bin/katman-wf-recorder')
                meta['backend']=backend
                self.tracks.append(Track(self.root, self.origin, "display:" + m["name"], meta, cmd, cfg["fps"]))
            self.running = True
            self._discover_audio(graph())
            # Do not announce a normal recording until both displays have frames.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                videos = [t for t in self.tracks if t.meta["type"] == "video"]
                if any(not t.alive() for t in videos):
                    failed = next(t for t in videos if not t.alive())
                    tail = (failed.path / "capture.log").read_text(errors="replace")[-1600:]
                    raise RuntimeError(f"{failed.meta['name']} could not start: {tail}")
                if all((t.path / "%08d.mkv.ts").exists() and (t.path / "00000000.mkv").exists() for t in videos):
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("Display capture did not start in time. Check the capture log.")
            self._discover_audio(graph())
            atomic_json(self.root / "session.json", {"created": datetime.now().isoformat(),
                        "sources": [{"id": t.id, "offset": t.offset, **t.meta} for t in self.tracks]})
        except BaseException:
            for t in self.tracks:
                t.stop()
            self.running = False
            shutil.rmtree(self.root,ignore_errors=True)
            raise

    def _discover_audio(self, objects):
        groups = audio_sources(objects)
        existing = {t.key: t for t in self.tracks if t.meta["type"] == "audio"}
        for key, group in groups.items():
            if group["name"].lower() in {"katman", "layer", "katman capture", "layer capture"}:
                continue
            if key not in existing:
                meta = {"type": "audio", **{k:group[k] for k in ("name","kind","application","stream_role") if k in group}}
                cmd = ["pw-record", "--target", "0", "--raw", "--format", "s16",
                       "--rate", "48000", "--channels", "2", "--latency", "20ms", "-P",
                       '{ node.name = "{node}" application.name = "Layer capture" node.autoconnect = false node.always-process = true }', "-"]
                track = Track(self.root, self.origin, key, meta, cmd, 0)
                self.tracks.append(track)
                existing[key] = track
        now=time.monotonic()-self.origin
        for key,track in existing.items():
            update_activity(track,groups.get(key,{}).get("active",False),now)
            update_presence(track,key in groups,now)
        ports = []
        nodes = {}
        for o in objects:
            if o.get("type") == "PipeWire:Interface:Node":
                nodes[o["id"]] = o.get("info", {}).get("props", {}).get("node.name")
            elif o.get("type") == "PipeWire:Interface:Port":
                props = o.get("info", {}).get("props", {})
                ports.append((o["id"], int(props.get("node.id", -1)), props.get("port.name", ""), props.get("port.direction")))
        actual_links={(int(o['info']['output-port-id']),int(o['info']['input-port-id'])) for o in objects if o.get('type')=='PipeWire:Interface:Link' and 'output-port-id' in o.get('info',{}) and 'input-port-id' in o.get('info',{})}
        for key,track in existing.items():
            track.links.intersection_update(actual_links)
            # A live stream may change its role without recreating its ports.
            # Disconnect only links owned by this capture track, never speaker routes.
            owned_inputs={id for id,nid,name,d in ports if nodes.get(nid)==track.name and d=='in'}
            allowed_outputs={id for id,nid,name,d in ports if nid in groups.get(key,{}).get('nodes',[]) and d=='out'}
            for pair in list(track.links):
                if pair[1] in owned_inputs and pair[0] not in allowed_outputs:
                    try:
                        run(['pw-link','-d',str(pair[0]),str(pair[1])],timeout=2);track.links.discard(pair)
                    except subprocess.SubprocessError:pass
        for key, group in groups.items():
            t = existing.get(key)
            if not t or not t.alive():
                continue
            mine = {name: id for id, nid, name, d in ports if nodes.get(nid) == t.name and d == "in"}
            for id, nid, name, direction in ports:
                if nid not in group["nodes"] or direction != "out" or name.startswith("playback_"):
                    continue
                if name.endswith(("_MONO", "_FC")):
                    dests = [mine.get("input_FL"), mine.get("input_FR")]
                elif name.endswith(("_FL", "_RL", "_SL")):
                    dests = [mine.get("input_FL")]
                elif name.endswith(("_FR", "_RR", "_SR")):
                    dests = [mine.get("input_FR")]
                else:
                    dests = []
                for dest in dests:
                    link = (id, dest)
                    if dest and link not in t.links:
                        try:
                            run(["pw-link", *(["-P"] if group["kind"]=="application" else []), str(id), str(dest)], timeout=2)
                            t.links.add(link)
                        except subprocess.SubprocessError:
                            pass
        # Forget links to removed PipeWire ports so recycled IDs can reconnect.
        valid = {p[0] for p in ports}
        for t in existing.values():
            t.links = {pair for pair in t.links if pair[0] in valid and pair[1] in valid}

    def _watch(self):
        tick = 0
        while True:
            time.sleep(.25)
            if not self.running or self.stopping:
                continue
            try:
                if tick % 4 == 0:
                    from .identity import focused_application
                    focused_application()
                objects = graph()
                with self.lock:
                    if not self.running or self.stopping:
                        continue
                    self._discover_audio(objects)
                    low_ram=shutil.disk_usage(self.root).free<MIN_FREE
                    low_disk=self.record_start is not None and shutil.disk_usage(CACHE).free<MIN_FREE
                    if low_ram or low_disk:
                        self.errors.append('Capture stopped safely because less than 512 MB remains in '+('RAM replay storage.' if low_ram else 'recording storage.'))
                        self.replay=False
                        if self.record_start is not None:self.toggle_record()
                        else:self._stop(clean=False)
                        continue
                    elapsed = time.monotonic() - self.origin
                    cutoff = elapsed - 306
                    if self.record_start is not None:
                        cutoff = min(cutoff, self.record_start - 3)
                    for t in self.tracks:
                        t.prune(cutoff, elapsed-306 if self.record_start is not None else None, self.archive_root)
                        if not t.alive() and not t.error_reported:
                            t.error_reported = True
                            tail = (t.path / "capture.log").read_text(errors="replace")[-1200:]
                            self.errors.append(f"{t.meta['name']}: the capture source stopped. {tail}")
                    tick += 1
                    if tick % 20 == 0:
                        atomic_json(self.root / "session.json", {"created": datetime.now().isoformat(),
                                    "sources": [{"id": t.id, "offset": t.offset, **t.meta} for t in self.tracks]})
            except Exception as e:
                with self.lock:
                    message = f"Source discovery: {e}"
                    if not self.errors or self.errors[-1] != message:
                        self.errors.append(message)

    def set_capture_state(self, kind, enabled):
        """Apply mirrored GSR state idempotently, including repeated menu refreshes."""
        if kind not in ("replay", "record") or not isinstance(enabled, bool):
            raise ValueError("Invalid capture state.")
        with self.lock:
            active = self.replay if kind == "replay" else self.record_start is not None
            if active == enabled:
                return self.status()
            return self.toggle_replay() if kind == "replay" else self.toggle_record()

    def toggle_replay(self):
        with self.lock:
            if self.replay:
                self.replay = False
                if self.record_start is None:
                    self._stop()
            else:
                self._start()
                self.replay = True
            return self.status()

    def toggle_record(self):
        with self.lock:
            if self.record_start is None:
                self._start()
                self.record_start = time.monotonic() - self.origin
                return self.status()
            end = time.monotonic() - self.origin
            start = self.record_start
            if not self.replay:
                self._stop(clean=False)
            project = self._snapshot(start, end, "recording")
            self.record_start = None
            if not self.running:
                shutil.rmtree(self.root, ignore_errors=True)
                if self.archive_root:shutil.rmtree(self.archive_root,ignore_errors=True)
            return {**self.status(), "saved": project}

    def save_replay(self, seconds):
        if seconds not in (60, 300):
            raise ValueError("Replay duration must be 60 or 300 seconds.")
        with self.lock:
            if not self.running or not self.replay:
                raise RuntimeError("Enable the replay buffer first.")
            end = time.monotonic() - self.origin
            return self._snapshot(max(0, end - seconds), end, "replay")

    def _snapshot(self, start, end, kind):
        from .identity import focused_application
        application = focused_application()
        if end - start < .5:
            raise RuntimeError("The recording is too short. Wait at least one second.")
        if self.running:
            # Wait for closed segments that cover the hotkey's original time.
            # This avoids copying a still-buffered final Matroska cluster; no
            # footage after the requested logical endpoint is exported.
            deadline = time.monotonic() + 4.5
            while time.monotonic() < deadline:
                covered = True
                for track in self.tracks:
                    if not track.alive() or track.offset >= end:
                        continue
                    rows = track.segments()
                    if not rows or track.offset + rows[-1][2] < end:
                        covered = False
                if covered:
                    break
                time.sleep(.05)
        cfg = settings()
        library = Path(cfg["library"]).expanduser()
        folder = library / application / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:5])
        folder.mkdir(parents=True)
        (folder / "media").mkdir()
        staging = folder / ".staging"
        staging.mkdir()
        project = {"format": "katman", "version": 1, "id": uuid.uuid4().hex,
                   "name": application, "application": application, "kind": kind,
                   "created": datetime.now().isoformat(), "duration": end - start,
                   "sources": [], "sections": [], "warnings": list(self.errors)}
        try:
            for t in self.tracks:
                if not active_in_window(t,start,end):
                    continue
                selected = [(f, s, e) for f, s, e in t.segments(include_active=True)
                            if f.exists() and t.offset + e > start and t.offset + s < end]
                if not selected:
                    project["warnings"].append(t.meta["name"] + ": no media is available in this range.")
                    continue
                copies = []
                for i, (file, s, e) in enumerate(selected):
                    copied = staging / f"{t.id}-{i}.mkv"
                    shutil.copyfile(file, copied)
                    if copied.stat().st_size > 500:
                        copies.append((copied, s))
                if not copies:
                    continue
                listing = staging / (t.id + ".txt")
                listing.write_text("\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p, s in copies))
                dest = folder / "media" / (t.id + ".mkv")
                try:
                    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                         "-safe", "0", "-i", listing, "-map", "0", "-c", "copy", dest], timeout=90)
                    info = probe(dest)
                    if not info.get("streams"):
                        raise ValueError("The media segment is empty")
                    first = t.offset + copies[0][1]
                    if t.meta.get("kind")=="application" and not has_audio_content(dest,max(0,start-first),end-max(start,first)):
                        dest.unlink(missing_ok=True)
                        continue
                    item = {"id": t.id, **t.meta, "file": str(dest.relative_to(folder)),
                            "timeline_start": max(0, first - start), "trim_in": max(0, start - first),
                            "media_duration": float(info["format"].get("duration", 0)), "enabled": True, "gain": 1.0}
                    if item["type"] == "video":
                        video = next(s for s in info["streams"] if s["codec_type"] == "video")
                        item.update(width=video["width"], height=video["height"])
                    project["sources"].append(item)
                    coverage = item["timeline_start"] + item["media_duration"] - item["trim_in"]
                    if coverage < project["duration"] - .25:
                        project["warnings"].append(f"{t.meta['name']}: the final {project['duration']-coverage:.2f} seconds could not be saved; the last video frame is held and missing audio is silent.")
                except Exception as e:
                    project["warnings"].append(f"{t.meta['name']}: the segment could not be saved: {e}")
            if not any(s["type"] == "video" for s in project["sources"]):
                raise RuntimeError("No display footage could be saved. Check the capture log.")
            atomic_json(folder / "project.katman", project)
            self.last_project = str(folder / "project.katman")
            return self.last_project
        except BaseException:
            # Preserve recoverable captured pieces and report the actual location.
            atomic_json(folder / "recovery.json", project)
            raise
        finally:
            if (folder / "project.katman").exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _stop(self, clean=True):
        self.stopping = True
        try:
            # Signal all producers together, then drain each encoder.
            for t in self.tracks:
                if t.producer.poll() is None:
                    t.producer.send_signal(signal.SIGINT)
            for t in self.tracks:
                t.stop()
            self.running = False
            if clean and self.root:
                shutil.rmtree(self.root, ignore_errors=True)
                if self.archive_root:shutil.rmtree(self.archive_root,ignore_errors=True)
        finally:
            self.stopping = False

    def shutdown(self):
        with self.lock:
            if self.record_start is not None:
                self.replay = False
                self.toggle_record()
            elif self.running:
                self._stop()
