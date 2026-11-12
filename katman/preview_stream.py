# SPDX-License-Identifier: GPL-3.0-only
"""Direct compositor playback: one JSON request in, packed RGBA frames out.

The native owner paces video and owns our process group. Do not create a new
session for FFmpeg or the audio player: cancelling playback must stop them all.
"""
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from .editor_worker import _load, _model
from .studio_model import duration, validate
from .studio_render import compile_graph, number
from .studio_jobs import preview_options


def stop(process):
    if process is None:return
    if process.poll() is None:
        try:process.terminate()
        except ProcessLookupError:pass
    try:process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=2)


def stream(payload, output=None):
    output=output if output is not None else sys.stdout.buffer
    path,project=_load(payload);model=_model(payload,project);validate(model,project)
    position=float(payload.get('position',0))
    options=preview_options(model,payload.get('width'),payload.get('height'),payload.get('fps'),payload.get('size'),payload.get('proxy',False))
    size=options['size'];fps=options['fps']
    if not math.isfinite(position) or position<0:raise ValueError('Invalid preview position.')
    end=duration(model)
    if payload.get('window') is not None:
        window=float(payload['window'])
        if not math.isfinite(window) or window<=0:raise ValueError('Invalid preview window.')
        end=min(end,position+window)
    if position>=end:return
    # Accurate effects always use the same graph as export, at playback size.
    audio=payload.get('audio',True) and shutil.which('paplay') is not None
    if payload.get('audio',True) and not audio:print('Preview audio unavailable: paplay is not installed.',file=sys.stderr)
    process=player=None;read_fd=write_fd=None;audio_thread=None
    ready=threading.Event();closed=threading.Event();audio_errors=[];player_lock=threading.Lock()
    with tempfile.TemporaryDirectory(prefix='layer-preview-stream-') as directory:
        options.update(video_only=not audio,pixel_format='rgba')
        args=compile_graph(path.parent,project,model,Path(directory),options,(position,end))
        # compile_graph supplies output maps and a duration for file exports.
        args=args[:-(6 if audio else 4)]
        args+=['-map','[vout]','-t',number(end-position),'-r',number(fps),'-threads','1','-c:v','rawvideo','-pix_fmt','rgba','-f','rawvideo','pipe:1']
        if audio:
            read_fd,write_fd=os.pipe()
            args+=['-map','[aout]','-t',number(end-position),'-c:a','pcm_s16le','-ar','48000','-ac','2','-f','s16le',f'pipe:{write_fd}']
        try:
            process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=sys.stderr,pass_fds=(() if write_fd is None else (write_fd,)),start_new_session=False)
            if write_fd is not None:os.close(write_fd);write_fd=None
            if audio:
                def play_audio():
                    nonlocal player,read_fd
                    try:
                        # Read before waiting so FFmpeg can finish its first
                        # video packet even if it emits audio first.
                        initial=os.read(read_fd,8192)
                        ready.wait()
                        with player_lock:
                            if closed.is_set():return
                            player=subprocess.Popen(['paplay','--raw','--format=s16le','--rate=48000','--channels=2','--latency-msec=50'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=sys.stderr,start_new_session=False)
                        player.stdin.write(initial)
                        while not closed.is_set():
                            chunk=os.read(read_fd,8192)
                            if not chunk:break
                            player.stdin.write(chunk)
                        player.stdin.close();player.wait()
                    except (OSError,ValueError) as error:
                        audio_errors.append(error)
                        # A missing audio server should not break video. Drain
                        # the second output to prevent the compositor blocking.
                        try:
                            while not closed.is_set() and os.read(read_fd,65536):pass
                        except OSError:pass
                    finally:
                        if read_fd is not None:
                            os.close(read_fd);read_fd=None
                audio_thread=threading.Thread(target=play_audio,daemon=True);audio_thread.start()
            frame_size=size[0]*size[1]*4
            while True:
                frame=process.stdout.read(frame_size)
                if not frame:break
                if len(frame)!=frame_size:raise RuntimeError('The preview returned an incomplete frame.')
                output.write(frame);output.flush();ready.set()
            if process.wait():raise RuntimeError('The preview compositor failed.')
            if audio_thread:audio_thread.join(timeout=2)
        finally:
            closed.set();ready.set();stop(process)
            with player_lock:stop(player)
            if process and process.stdout:process.stdout.close()
            if write_fd is not None:os.close(write_fd)
            if audio_thread:audio_thread.join(timeout=2)
            elif read_fd is not None:os.close(read_fd)
    if audio_errors:print('Preview audio unavailable; video continued.',file=sys.stderr)


def main():
    def terminated(_signum,_frame):raise InterruptedError('Preview cancelled.')
    signal.signal(signal.SIGTERM,terminated)
    try:
        payload=json.load(sys.stdin)
        if not isinstance(payload,dict):raise ValueError('Expected a preview request object.')
        stream(payload)
        return 0
    except (BrokenPipeError,InterruptedError,KeyboardInterrupt):return 0
    except Exception as error:
        print(str(error),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
