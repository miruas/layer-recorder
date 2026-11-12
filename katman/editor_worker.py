# SPDX-License-Identifier: GPL-3.0-only
"""Headless JSON-lines worker for the native editor boundary.

The Rust editor owns the window and the document.  This module exposes the
existing Python renderer/analysis pipeline to it without importing Qt or
opening any GUI.  One request object is read from stdin; progress messages and
one final result are written to stdout.  Human-readable diagnostics go to
stderr.
"""
import copy
import hashlib
import json
import math
import re
import signal
import sys
import threading
import tempfile
from pathlib import Path

from .common import CACHE, load_project
from .studio_jobs import (
    build_proxies,
    preview_file,
    preview_options,
    stabilize,
    track_region,
    transcribe,
)
from .studio_model import (
    duration,
    from_project,
    import_sources,
    parse_srt,
    validate,
    write_srt,
)
from .studio_render import export_studio, media_fingerprint, preview_project_snapshot

__all__ = ["request", "main"]


def _option(payload, names, default=None):
    """Read a value from the request or its nested ``options`` object."""
    options = payload.get("options")
    if not isinstance(options, dict):
        options = {}
    for name in (names,) if isinstance(names, str) else names:
        if name in payload:
            return payload[name]
        if name in options:
            return options[name]
    return default


def _number(value, label, minimum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number.")
    if minimum is not None and result < minimum:
        raise ValueError(f"{label} cannot be less than {minimum}.")
    return result


def _load(payload, check_sources=True):
    raw = _option(payload, "path")
    if raw in (None, ""):
        raise ValueError("This operation requires a project path.")
    snapshot=payload.get("project_snapshot")
    if snapshot is not None:
        path=Path(raw).expanduser().resolve()
        if path.is_dir():path=path/"project.katman"
        project=preview_project_snapshot(path,snapshot)
    else:
        path, project = load_project(raw)
    path = Path(path)
    if check_sources:
        _check_sources(path, project)
    return path, project


def _check_sources(path, project):
    root = path.parent.resolve()
    for source in project.get("sources", []):
        try:
            file = (root / source["file"]).resolve()
        except (KeyError, TypeError):
            raise ValueError("A project source has no media file.") from None
        if not file.is_relative_to(root):
            raise ValueError("Media files must be inside the project folder.")
        if not file.is_file():
            raise ValueError(f"Source media is missing: {source.get('file', '')}")


def _model(payload, project):
    raw = payload.get("model")
    if raw is None:
        raw = project.get("studio")
    if raw is None:
        raw = from_project(project)
    if not isinstance(raw, dict):
        raise ValueError("The timeline model is invalid.")
    return copy.deepcopy(raw)


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Operation cancelled.")


def _frame(payload, progress, cancel):
    path, project = _load(payload)
    model = _model(payload, project)
    validate(model, project)
    _cancelled(cancel)
    position = _number(_option(payload, "position", 0), "The frame position", 0)
    options=preview_options(model,_option(payload,'width'),_option(payload,'height'),_option(payload,'fps'),_option(payload,'size'),_option(payload,'proxy',False))
    options['pixel_format']='rgba'
    signature = json.dumps(model, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    project_signature = json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    key = hashlib.sha256(
        (
            str(path.resolve())
            + "\0"
            + project_signature
            + "\0"
            + signature
            + media_fingerprint(path, project, model)
            + "\0"
            + f"{position:.6f}"
            + "\0"
            + json.dumps(options,sort_keys=True)
        ).encode()
    ).hexdigest()[:32]
    root = Path(CACHE) / "studio-native-frames"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / (key + ".png")
    if destination.is_file() and destination.stat().st_size > 0:
        return {"file": str(destination)}
    if destination.exists():
        destination.unlink()
    export_studio(
        path,
        destination,
        options,
        progress,
        cancel,
        model=model,
        window=(position, position + .05),
        frame=True,
        project_snapshot=project,
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("The preview frame could not be written.")
    return {"file": str(destination)}


def _preview(payload, progress, cancel):
    path, project = _load(payload)
    model = _model(payload, project)
    validate(model, project)
    _cancelled(cancel)
    options=preview_options(model,_option(payload,'width'),_option(payload,'height'),_option(payload,'fps'),_option(payload,'size'),_option(payload,'proxy',False))
    return {"file": str(preview_file(path, model, cancel, progress, options=options))}


def _export(payload, progress, cancel):
    path, project = _load(payload)
    model = _model(payload, project)
    validate(model, project)
    _cancelled(cancel)
    output = _option(payload, "output")
    if output in (None, ""):
        raise ValueError("Export requires an output file.")
    options = _option(payload, "options", {})
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("Export options must be an object.")
    return {"file": str(export_studio(path, output, copy.deepcopy(options), progress, cancel, model=model))}


def _import(payload, progress, cancel):
    path, loaded = _load(payload, check_sources=False)
    project = payload.get("project")
    if project is None:
        project = loaded
    if not isinstance(project, dict):
        raise ValueError("The project model is invalid.")
    project = copy.deepcopy(project)
    _check_sources(path, project)
    model = _model(payload, project)
    validate(model, project, allow_empty=True)
    _cancelled(cancel)
    source = _option(payload, ("source", "source_path", "input"))
    if source in (None, ""):
        raise ValueError("Import requires a media or project source.")
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError(f"Source media is missing: {source}")
    overlay = bool(_option(payload, "overlay", False))
    at = _number(_option(payload, "position", _option(payload, "at", 0)), "The import position", 0) if overlay else duration(model)
    import_sources(path, project, model, source_path, at)
    _cancelled(cancel)
    validate(model, project)
    return {"project": project, "model": model}


def _proxies(payload, progress, cancel):
    path, _ = _load(payload)
    _cancelled(cancel)
    return {"count": build_proxies(path, cancel, progress)}


def _stabilize(payload, progress, cancel):
    path, project = _load(payload)
    clip = _option(payload, "clip")
    if not isinstance(clip, dict):
        raise ValueError("Stabilization requires a clip.")
    source = next((s for s in project.get("sources", []) if s.get("id") == clip.get("source_id")), None)
    if source is None:
        raise ValueError("The clip does not belong to this project.")
    if source.get("type") != "video":
        raise ValueError("Select a video clip to stabilize.")
    _cancelled(cancel)
    return {"file": str(stabilize(path, copy.deepcopy(clip), cancel, progress))}


def _track(payload, progress, cancel):
    path, project = _load(payload)
    model = _model(payload, project)
    validate(model, project)
    annotation = _option(payload, "annotation")
    if not isinstance(annotation, dict):
        raise ValueError("Tracking requires an annotation.")
    if not annotation.get("id"):
        raise ValueError("The annotation needs an id.")
    for field in ("start", "end", "x", "y", "w", "h"):
        if field not in annotation:
            raise ValueError(f"The annotation is missing {field}.")
    _cancelled(cancel)
    return {"keys": track_region(path, model, copy.deepcopy(annotation), cancel, progress)}


def _transcribe(payload, progress, cancel):
    path, project = _load(payload)
    model = _model(payload, project)
    validate(model, project)
    _cancelled(cancel)
    language = _option(payload, "language", "auto") or "auto"
    return {"captions": transcribe(path, model, cancel, progress, str(language))}


def _import_srt(payload, progress, cancel):
    text = _option(payload, "text")
    if text is None:
        source = _option(payload, ("file", "source", "source_path", "input"))
        if source in (None, ""):
            raise ValueError("Subtitle import requires text or a subtitle file.")
        path = Path(source).expanduser()
        if not path.is_absolute():
            raw = _option(payload, "path")
            if raw not in (None, ""):
                path = Path(load_project(raw)[0]).parent / path
        text = path.read_text(encoding="utf-8-sig")
    if not isinstance(text, str):
        raise ValueError("Subtitle text must be a string.")
    _cancelled(cancel)
    subtitles = parse_srt(text)
    if not subtitles:
        raise ValueError("No valid subtitle entries were found in this file.")
    return {"subtitles": subtitles}


def _export_srt(payload, progress, cancel):
    output = _option(payload, "output")
    if output in (None, ""):
        raise ValueError("Subtitle export requires an output file.")
    destination = Path(output).expanduser()
    if destination.exists():
        raise ValueError("Choose a new filename to preserve the existing file.")
    subtitles = payload.get("subtitles")
    if subtitles is None:
        model = payload.get("model")
        if model is None:
            raw = _option(payload, "path")
            if raw not in (None, ""):
                project = load_project(raw)[1]
                model = _model(payload, project)
        if not isinstance(model, dict):
            raise ValueError("Subtitle export requires a timeline model or subtitles.")
        subtitles = model.get("subtitles", [])
    if not isinstance(subtitles, list):
        raise ValueError("The subtitle entries are invalid.")
    for caption in subtitles:
        if not isinstance(caption, dict) or not isinstance(caption.get("text"), str) or "start" not in caption or "end" not in caption:
            raise ValueError("The subtitle entries are invalid.")
    _cancelled(cancel)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(write_srt(subtitles), encoding="utf8")
    return {"file": str(destination)}


def _waveforms(payload, progress, cancel):
    from .common import atomic_json
    from .studio_render import quote, run_process
    path,project=_load(payload)
    root=CACHE/'studio-waveforms';root.mkdir(parents=True,exist_ok=True)
    sources=[source for source in project['sources'] if source['type']=='audio']
    items=[]
    for index,source in enumerate(sources):
        _cancelled(cancel)
        media=path.parent/source['file'];stat=media.stat()
        key=hashlib.sha256((str(media)+str(stat.st_size)+str(stat.st_mtime_ns)).encode()).hexdigest()
        cached=root/(key+'.json')
        if cached.is_file():
            data=json.loads(cached.read_text());data['source_id']=source['id'];items.append(data);continue
        seconds=max(.01,float(source.get('media_duration',project.get('duration',1))))
        bins=min(1024,max(1,int(seconds*48000/128)));samples=max(128,min(480000,math.ceil(seconds*48000/bins)))
        peaks=[0.0]*bins
        with tempfile.TemporaryDirectory(prefix='waveform-',dir=root) as directory:
            directory=Path(directory);metadata=directory/'peaks.txt'
            filters=f"asetnsamples=n={samples},astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level,ametadata=print:key=lavfi.astats.Overall.Peak_level:file='{quote(metadata)}'"
            run_process(['ffmpeg','-v','error','-i',str(media),'-vn','-af',filters,'-threads','1','-progress','pipe:1','-f','null','-'],directory/'analysis.log',cancel)
            timestamp=0.0
            with metadata.open() as lines:
                for line in lines:
                    if line.startswith('frame:'):
                        match=re.search(r'pts_time:([0-9.eE+-]+)',line)
                        if match:timestamp=float(match[1])
                    elif line.startswith('lavfi.astats.Overall.Peak_level='):
                        value=float(line.split('=',1)[1]);peak=10**(value/20) if math.isfinite(value) else 0
                        slot=min(bins-1,max(0,int(timestamp/seconds*bins)));peaks[slot]=max(peaks[slot],min(1.0,peak))
        data={'source_id':source['id'],'duration':seconds,'peaks':peaks};atomic_json(cached,data);items.append(data)
        if progress:progress((index+1)/max(1,len(sources)))
    return {'items':items}


def _batch_export(payload, progress, cancel):
    paths=payload.get('paths')
    if not isinstance(paths,list) or not paths or any(not isinstance(path,str) for path in paths):
        raise ValueError('Select at least one recording for batch export.')
    output=Path(payload.get('output_directory','')).expanduser()
    if not str(payload.get('output_directory','')).strip():raise ValueError('Choose an export folder.')
    output.mkdir(parents=True,exist_ok=True)
    options=copy.deepcopy(payload.get('options') or {})
    if not isinstance(options,dict):raise ValueError('Export options must be an object.')
    files=[];skipped=[]
    for index,filename in enumerate(paths):
        _cancelled(cancel)
        path,project=load_project(filename);model=from_project(project)
        validate(model,project)
        name=re.sub(r'[^\w .-]+','_',str(project.get('name') or 'Recording')).strip(' .')[:80] or 'Recording'
        date=re.sub(r'[^0-9T-]','-',str(project.get('created') or path.parent.name))[:30]
        token=hashlib.sha256(str(path).encode()).hexdigest()[:8]
        destination=output/(name+'-'+date+'-'+token+('.m4a' if options.get('audio_only') else '.mp4'))
        if destination.exists():
            skipped.append(str(destination))
        else:
            update=(lambda value, i=index: progress((i+value)/len(paths))) if progress else None
            files.append(str(export_studio(path,destination,options,update,cancel,model=model)))
        if progress:progress((index+1)/len(paths))
    return {'files':files,'skipped':skipped}


def _dispatch(payload, progress, cancel):
    operation = payload.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("The request has no operation.")
    name = operation.strip().lower().replace("-", "_")
    if name in ("voiceover", "record_voiceover"):
        raise RuntimeError("Voiceover recording is not supported by the headless worker.")
    if name == "waveforms":
        return _waveforms(payload, progress, cancel)
    if name == "batch_export":
        return _batch_export(payload, progress, cancel)
    if name == "frame":
        return _frame(payload, progress, cancel)
    if name == "preview":
        return _preview(payload, progress, cancel)
    if name == "export":
        return _export(payload, progress, cancel)
    if name == "import":
        return _import(payload, progress, cancel)
    if name == "proxies":
        return _proxies(payload, progress, cancel)
    if name == "stabilize":
        return _stabilize(payload, progress, cancel)
    if name == "track":
        return _track(payload, progress, cancel)
    if name == "transcribe":
        return _transcribe(payload, progress, cancel)
    if name in ("import_srt", "import_subtitles"):
        return _import_srt(payload, progress, cancel)
    if name in ("export_srt", "export_subtitles"):
        return _export_srt(payload, progress, cancel)
    raise ValueError(f"Unknown worker operation: {operation}")


def request(payload, progress=None, cancel=None):
    """Execute one worker request and return ``{ok, data}`` or ``{ok, error}``."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("The worker request must be a JSON object.")
        data = _dispatch(payload, progress, cancel or threading.Event())
        return {"ok": True, "data": data}
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return {"ok": False, "error": message}


def main():
    cancel = threading.Event()

    def stop(*_):
        cancel.set()

    previous = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous.append((signum, signal.getsignal(signum)))
            signal.signal(signum, stop)
        except (ValueError, OSError):
            pass

    def emit(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        value = max(0.0, min(1.0, value))
        sys.stdout.write(json.dumps({"event": "progress", "value": value}, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    result = {"ok": False, "error": "No worker request was received."}
    try:
        line = sys.stdin.readline()
        if line.strip():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                result = {"ok": False, "error": f"Invalid JSON request: {exc.msg}."}
            else:
                result = request(payload, emit, cancel)
    finally:
        for signum, handler in previous:
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    if not result.get("ok"):
        sys.stderr.write(str(result.get("error", "The operation failed.")) + "\n")
        sys.stderr.flush()
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
