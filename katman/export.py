import json
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from .common import load_project
from .editing import scene_layout, scene_size, crop_geometry, validate_effects, annotation_image


def sections_for(project):
    if project.get("sections"):
        return project["sections"]
    return [{"start": 0, "end": project["duration"],
             "enabled_ids": [s["id"] for s in project["sources"] if s.get("enabled", True)],
             "gains": {s["id"]: s.get("gain", 1) for s in project["sources"]}}]


def layout(sources):
    if not sources:
        return 1920, 1080, {}
    if len(sources) == 1:
        s = sources[0]
        return s["width"], s["height"], {s["id"]: (0, 0, s["width"], s["height"])}
    left = min(s.get("x", 0) for s in sources)
    top = min(s.get("y", 0) for s in sources)
    rects = {s["id"]: (s.get("x", 0) - left, s.get("y", 0) - top,
             s.get("logical_width", s["width"]), s.get("logical_height", s["height"])) for s in sources}
    return max(x+w for x,y,w,h in rects.values()), max(y+h for x,y,w,h in rects.values()), rects


def validate(project):
    sections = sections_for(project)
    ids = {s["id"] for s in project["sources"]}
    if not sections:
        raise ValueError("There are no clips to export.")
    for section in sections:
        validate_effects(section, ids)
        if not 0 <= section["start"] < section["end"] <= project["duration"] + .05:
            raise ValueError("Check the clip in and out points.")
        if not set(section["enabled_ids"]).issubset(ids):
            raise ValueError("A selected source is missing from this clip.")
        for gain in section.get("gains", {}).values():
            if not 0 <= float(gain) <= 2:
                raise ValueError("Audio level must be between 0% and 200%.")
    return sections


def section_command(folder, project, section, output, size, fps=60, annotation_path=None):
    speed = float(section.get("speed", 1))
    duration = (section["end"] - section["start"]) / speed
    enabled = [s for s in project["sources"] if s["id"] in section["enabled_ids"]]
    order=section.get("layer_order", [s["id"] for s in enabled])
    enabled.sort(key=lambda s: order.index(s["id"]) if s["id"] in order else len(order))
    videos = [s for s in enabled if s["type"] == "video"]
    cw, ch, rects = scene_layout(videos, section)
    ow, oh = size
    cx,cy,crop_w,crop_h = crop_geometry(cw,ch,section)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-filter_complex_threads", "2"]
    filters = [f"color=c=black:s={cw}x{ch}:r={fps}:d={duration:.6f}[base]"]
    video_prev = "base"
    audio_labels = []
    for i, source in enumerate(enabled):
        local = section["start"] - source.get("timeline_start", 0) + source.get("trim_in", 0)
        delay = max(0, source.get("timeline_start", 0) - section["start"])
        args += ["-ss", f"{max(0,local):.6f}", "-i", str(folder / source["file"])]
        if source["type"] == "video":
            x, y, w, h = rects[source["id"]]
            w, h = max(2, round(w/2)*2), max(2, round(h/2)*2)
            x,y = round(x),round(y)
            privacy = "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill," if source["id"] in section.get("blackout_ids",[]) else ""
            filters.append(f"[{i}:v]setpts=(PTS-STARTPTS)/{speed},{privacy}scale={w}:{h}:flags=lanczos,setsar=1,"
                           f"tpad=start_duration={delay/speed:.6f}:start_mode=add:stop_duration={duration:.6f}:stop_mode=clone,"
                           f"trim=duration={duration:.6f}[v{i}]")
            new = f"mix{i}"
            filters.append(f"[{video_prev}][v{i}]overlay={x}:{y}:eof_action=pass:shortest=0[{new}]")
            video_prev = new
        else:
            gain = float(section.get("gains", {}).get(source["id"], source.get("gain", 1)))
            filters.append(f"[{i}:a]asetpts=PTS-STARTPTS,atempo={speed ** .5},atempo={speed ** .5},volume={gain},adelay={round(delay/speed*1000)}:all=1,"
                           f"apad,atrim=duration={duration:.6f}[a{i}]")
            audio_labels.append(f"[a{i}]")
    if audio_labels:
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,alimiter=limit=0.95:latency=1[aout]")
    else:
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[aout]")
    filters.append(f"[{video_prev}]crop={crop_w}:{crop_h}:{cx}:{cy}[cropped]")
    video_prev = "cropped"
    if annotation_path:
        args += ["-loop", "1", "-i", str(annotation_path)]
        filters.append(f"[cropped][{len(enabled)}:v]overlay=0:0:shortest=0[marked]")
        video_prev = "marked"
    filters.append(f"[{video_prev}]scale={ow}:{oh}:force_original_aspect_ratio=decrease,pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[vout]")
    args += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
             "-t", f"{duration:.6f}", "-r", str(fps), "-c:v", "libx264", "-preset", "fast",
             "-crf", "20", "-threads", "4", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)]
    return args


def export_project(path, destination, progress=None, cancel=None, preview=False):
    path, project = load_project(path)
    if project.get("studio"):
        from .studio_render import export_studio
        return export_studio(path,destination,{"size":(960,640)} if preview else None,progress,cancel)
    sections = validate(project)
    dest = Path(destination).expanduser().resolve()
    if dest == path or dest.is_relative_to(path.parent / "media"):
        raise ValueError("Exports cannot overwrite the original project or its media.")
    if dest.exists():
        raise ValueError("This file already exists. Choose a new filename.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    sizes = [scene_size(project, sec) for sec in sections]
    w, h = max(s[0] for s in sizes), max(s[1] for s in sizes)
    if preview:
        factor = min(1, 960/w, 640/h)
        w, h = w*factor, h*factor
    size = (max(2, round(w/2)*2), max(2, round(h/2)*2))
    temp_output = dest.with_name("." + dest.stem + "-" + uuid.uuid4().hex[:8] + ".mp4")
    try:
        with tempfile.TemporaryDirectory(prefix="katman-export-", dir=dest.parent) as td:
            td = Path(td)
            parts = []
            total = sum((s["end"]-s["start"])/s.get("speed",1) for s in sections)
            finished = 0
            for index, sec in enumerate(sections):
                if cancel and cancel.is_set():
                    raise RuntimeError("Export cancelled.")
                part = td / f"part-{index:04d}.mp4"
                overlay = None
                if sec.get("annotations"):
                    overlay = td / f"drawing-{index}.png"
                    annotation_image(sec, scene_size(project, sec), overlay)
                cmd = section_command(path.parent, project, sec, part, size, 30 if preview else 60, overlay)
                # FFmpeg progress is separate from diagnostic output and never blocks the UI.
                cmd[-1:-1] = ["-progress", "pipe:1", "-stats_period", "0.25"]
                with open(td / "error.log", "w+") as err:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
                    def watch_cancel():
                        while p.poll() is None:
                            if cancel and cancel.wait(.2):
                                p.terminate()
                                return
                            if not cancel:
                                return
                    threading.Thread(target=watch_cancel, daemon=True).start()
                    for line in p.stdout:
                        if progress and line.startswith("out_time_us="):
                            try:
                                progress(min(.99, (finished + int(line.split("=")[1])/1e6)/total))
                            except ValueError:
                                pass
                    p.stdout.close()
                    p.wait()
                    if cancel and cancel.is_set():
                        raise RuntimeError("Export cancelled.")
                    if p.returncode:
                        err.seek(0)
                        raise RuntimeError("Video export failed: " + err.read()[-3000:])
                parts.append(part)
                finished += (sec["end"]-sec["start"])/sec.get("speed",1)
            listing = td / "parts.txt"
            listing.write_text("\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in parts))
            from .common import run
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", listing, "-c", "copy", "-movflags", "+faststart", temp_output], timeout=180)
            # Never silently replace a file created while this export was running.
            os.link(temp_output, dest)
            temp_output.unlink()
            if progress:
                progress(1)
            return str(dest)
    finally:
        temp_output.unlink(missing_ok=True)
