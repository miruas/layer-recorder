# SPDX-License-Identifier: GPL-3.0-only
"""Cancellable native media analysis, proxies, tracking and offline captions."""
import copy
import functools
import hashlib
import json
import math
import os
import selectors
import time
from contextlib import contextmanager
import subprocess
import tempfile
from pathlib import Path
from .common import APP,CACHE,load_project,settings
from .studio_model import clip_duration,parse_srt
from .studio_render import export_studio,run_process,media_fingerprint


def preview_options(model,max_width=960):
    factor=min(1,max_width/model['width'],640/model['height']);return {'size':(max(2,round(model['width']*factor/2)*2),max(2,round(model['height']*factor/2)*2)),'fps':24,'quality':28,'preset':'ultrafast','proxy':True}


def preview_file(path,model,cancel=None,progress=None,width=960):
    path,project=load_project(path)
    root=CACHE/'studio-previews';root.mkdir(exist_ok=True)
    key=hashlib.sha256((str(path)+json.dumps(model,sort_keys=True)+str(width)+media_fingerprint(path,project,model)).encode()).hexdigest()[:24];dest=root/(key+'.mp4')
    if not dest.exists():export_studio(path,dest,preview_options(model,width),progress,cancel,model=model)
    return str(dest)


def build_proxies(path,cancel=None,progress=None):
    path,project=load_project(path);root=CACHE/'studio-proxies';root.mkdir(exist_ok=True);videos=[s for s in project['sources'] if s['type']=='video']
    for index,s in enumerate(videos):
        file=path.parent/s['file'];key=hashlib.sha256((str(file.resolve())+str(file.stat().st_mtime_ns)).encode()).hexdigest()[:20];dest=root/(key+'.mp4')
        if dest.exists():continue
        partial=root/(key+'-working.mp4')
        args=['ffmpeg','-v','error','-y','-i',str(file),'-an','-vf',"scale=w='min(960,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",'-c:v','libx264','-preset','ultrafast','-crf','28','-threads','2','-progress','pipe:1',str(partial)]
        try:run_process(args,root/(key+'.log'),cancel,lambda p:progress((index+p)/len(videos)) if progress else None,s.get('media_duration',1));partial.replace(dest)
        finally:partial.unlink(missing_ok=True)
    return len(videos)


@functools.lru_cache(maxsize=1)
def stabilization_fileformat_option():
    # Older distro FFmpeg builds always write ASCII and have no fileformat
    # option. Newer builds default to binary, so request ASCII explicitly.
    help_text=subprocess.run(['ffmpeg','-hide_banner','-h','filter=vidstabdetect'],
                             capture_output=True,text=True,timeout=10)
    return ':fileformat=ascii' if 'fileformat' in help_text.stdout else ''


def stabilize(path,clip,cancel=None,progress=None):
    path,project=load_project(path);source=next(s for s in project['sources'] if s['id']==clip['source_id']);root=CACHE/'studio-stabilization';root.mkdir(exist_ok=True)
    key=hashlib.sha256((str(path)+source['file']+str(clip['in'])+str(clip['length'])).encode()).hexdigest()[:24];result=root/(key+'.trf')
    from .studio_render import quote
    # libvidstab 1.1.1's binary header parser consumes accuracy=9 as a TAB.
    # ASCII avoids that upstream parsing bug and stays readable across releases.
    args=['ffmpeg','-v','error','-ss',str(clip['in']),'-i',str(path.parent/source['file']),'-t',str(clip['length']),'-an','-vf',f"format=yuv420p,vidstabdetect=shakiness=5:accuracy=9{stabilization_fileformat_option()}:result='{quote(result)}'",'-threads','2','-progress','pipe:1','-f','null','-']
    run_process(args,root/(key+'.log'),cancel,progress,clip['length']);return str(result)


def transcribe(path,model,cancel=None,progress=None,language='auto'):
    cfg=settings();engine=Path(cfg['speech_binary']).expanduser() if cfg['speech_binary'] else APP/'speech/whisper-cli';weights=Path(cfg['speech_model']).expanduser() if cfg['speech_model'] else (CACHE/'speech/ggml-base.bin' if (CACHE/'speech/ggml-base.bin').is_file() else APP/'speech/ggml-base.bin')
    if not engine.is_file() or not weights.is_file():raise RuntimeError('The offline speech engine/model is missing. Run layer download-model, or configure speech_binary/speech_model for your local engine and model.')
    with tempfile.TemporaryDirectory(prefix='layer-captions-') as directory:
        root=Path(directory);mix=root/'mix.wav';export_studio(path,mix,{'audio_only':True},lambda p:progress(p*.25) if progress else None,cancel,model)
        wav=root/'speech.wav';run_process(['ffmpeg','-v','error','-y','-i',str(mix),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],root/'convert.log',cancel)
        args=[str(engine),'-m',str(weights),'-f',str(wav),'-l',language,'-t','4','-osrt','-of',str(root/'captions')]
        run_process(args,root/'speech.log',cancel)
        captions=parse_srt((root/'captions.srt').read_text())
        if progress:progress(1)
        return captions


@contextmanager
def _tracking_frames(video, cancel=None):
    """Decode through the known CLI, never a second FFmpeg hidden in OpenCV."""
    import numpy as np
    info=subprocess.run(['ffprobe','-v','error','-select_streams','v:0',
                         '-show_entries','stream=width,height','-of','json',str(video)],
                        capture_output=True,text=True,check=True,timeout=10)
    stream=json.loads(info.stdout)['streams'][0];w,h=int(stream['width']),int(stream['height'])
    if not (1<=w<=16384 and 1<=h<=16384):raise ValueError('Invalid tracking preview dimensions.')
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(['ffmpeg','-v','error','-i',str(video),'-an','-sn','-dn',
                                  '-threads','1','-pix_fmt','bgr24','-f','rawvideo','pipe:1'],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=errors,
                                 # Stay in the media worker's owned process group,
                                 # so cancelling that job also stops this decoder.
                                 start_new_session=False)
        selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
        os.set_blocking(process.stdout.fileno(),False)
        def frames():
            pending=bytearray();frame_size=w*h*3;deadline=time.monotonic()+15
            while True:
                if cancel and cancel.is_set():raise RuntimeError('Tracking cancelled.')
                if time.monotonic()>deadline:raise RuntimeError('Tracking decoder did not respond in time.')
                if not selector.select(.1):continue
                chunk=os.read(process.stdout.fileno(),min(262144,frame_size-len(pending)))
                if not chunk:
                    if pending:raise RuntimeError('Tracking decoder returned an incomplete frame.')
                    if process.wait(timeout=5):
                        errors.seek(0);raise RuntimeError('Could not decode tracking footage. '+errors.read()[-1200:].decode(errors='replace'))
                    return
                pending.extend(chunk)
                if len(pending)==frame_size:
                    yield np.frombuffer(bytes(pending),dtype=np.uint8).reshape(h,w,3)
                    pending.clear();deadline=time.monotonic()+15
        try:yield w,h,frames()
        finally:
            selector.close();process.stdout.close()
            if process.poll() is None:
                try:process.terminate()
                except ProcessLookupError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:process.kill()
                except ProcessLookupError:pass
                process.wait(timeout=2)


def track_region(path,model,annotation,cancel=None,progress=None):
    import cv2,numpy as np
    clean=copy.deepcopy(model);clean['annotations']=[a for a in clean['annotations'] if a['id']!=annotation['id']]
    clean['subtitles']=[];start,end=annotation['start'],annotation['end'];options=preview_options(model,640);options['fps']=10
    with tempfile.TemporaryDirectory(prefix='layer-track-') as directory:
        video=Path(directory)/'tracking.mp4';export_studio(path,video,options,lambda p:progress(p*.5) if progress else None,cancel,clean,(start,end))
        with _tracking_frames(video,cancel) as (w,h,frames):
            frame=next(frames,None)
            if frame is None:raise RuntimeError('Could not decode tracking footage.')
            sx,sy=w/model['width'],h/model['height'];x,y,rw,rh=annotation['x']*sx,annotation['y']*sy,annotation['w']*sx,annotation['h']*sy
            previous=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY);mask=np.zeros_like(previous);mask[max(0,int(y)):min(h,int(y+rh)),max(0,int(x)):min(w,int(x+rw))]=255
            points=cv2.goodFeaturesToTrack(previous,100,.01,4,mask=mask)
            if points is None or len(points)<4:raise RuntimeError('This region has too little visual detail to track. Choose a larger, textured region.')
            keys={'x':[[0,annotation['x']]],'y':[[0,annotation['y']]],'w':[[0,annotation['w']]],'h':[[0,annotation['h']]]};total=max(1,math.ceil((end-start)*10));stride=max(1,math.ceil(total/96))
            index=0
            for index,frame in enumerate(frames,1):
                gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY);tracked,status,err=cv2.calcOpticalFlowPyrLK(previous,gray,points,None)
                if tracked is None or status is None:raise RuntimeError(f'Tracking lost at {start+index/10:.1f}s. No changes were applied.')
                good=points[status.ravel()==1];new=tracked[status.ravel()==1]
                if len(good)<4:raise RuntimeError(f'Tracking lost at {start+index/10:.1f}s. Shorten the range or add manual keyframes.')
                transform,inliers=cv2.estimateAffinePartial2D(good,new,method=cv2.RANSAC)
                if transform is None:raise RuntimeError('Tracking could not estimate stable motion. No changes were applied.')
                corners=np.array([[[x,y],[x+rw,y],[x,y+rh],[x+rw,y+rh]]],dtype=np.float32);corners=cv2.transform(corners,transform)[0]
                x,y=np.min(corners,axis=0);right,bottom=np.max(corners,axis=0);rw,rh=right-x,bottom-y
                if index%stride==0:
                    for key,value in zip(('x','y','w','h'),(x/sx,y/sy,rw/sx,rh/sy)):keys[key].append([index/10,float(value)])
                previous=gray;points=new.reshape(-1,1,2)
                if progress:progress(min(1,.5+.5*index/total))
            if index and index%stride:
                for key,value in zip(('x','y','w','h'),(x/sx,y/sy,rw/sx,rh/sy)):keys[key].append([index/10,float(value)])
            if progress:progress(1)
            return keys
