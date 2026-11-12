# SPDX-License-Identifier: GPL-3.0-only
"""One FFmpeg compositor for timeline previews, audio mixes and final exports."""
import copy
import functools
import hashlib
import json
import math
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from .common import CACHE,load_project,settings
from .studio_model import duration,clip_duration,rate_segments,validate


def number(x):return f'{float(x):.7f}'

def quote(value):return str(value).replace('\\','\\\\').replace("'", "'\\''").replace(':','\\:')


def curve(item,key,var='t',default=0):
    pts=sorted(item.get('keys',{}).get(key,[]))
    if not pts:return number(item.get(key,default))
    expr=number(pts[-1][1])
    for a,b in reversed(list(zip(pts,pts[1:]))):
        lerp=f'({number(a[1])}+({number(b[1]-a[1])})*({var}-{number(a[0])})/{number(max(.000001,b[0]-a[0]))})'
        expr=f'if(lt({var},{number(b[0])}),{lerp},{expr})'
    return f'if(lt({var},{number(pts[0][0])}),{number(pts[0][1])},{expr})'


def run_process(args,log,cancel=None,progress=None,total=1):
    with open(log,'w+') as errors:
        p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=errors,text=True)
        def watch():
            if cancel:
                while p.poll() is None:
                    if cancel.wait(.1):p.terminate();return
        threading.Thread(target=watch,daemon=True).start()
        for line in p.stdout:
            if progress and line.startswith('out_time_us='):
                try:progress(min(.995,float(line.split('=')[1])/1e6/max(.001,total)))
                except ValueError:pass
        p.stdout.close();p.wait()
        if cancel and cancel.is_set():raise RuntimeError('Operation cancelled.')
        if p.returncode:
            errors.seek(0);raise RuntimeError('Media processing failed: '+errors.read()[-3500:])


@functools.lru_cache(maxsize=1)
def graph_file_option():
    help_text=subprocess.run(['ffmpeg','-hide_banner','-h','full'],capture_output=True,text=True,timeout=10).stdout
    return '-filter_complex_script' if '-filter_complex_script' in help_text else '-/filter_complex'


@functools.lru_cache(maxsize=64)
def font_file(name):
    return subprocess.check_output(['fc-match','-f','%{file}',name],text=True).strip()


@functools.lru_cache(maxsize=128)
def source_frame_interval(file,mtime,size):
    info=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=avg_frame_rate','-of','json',file],text=True,timeout=10)
    rate=json.loads(info)['streams'][0]['avg_frame_rate'];a,b=rate.split('/')
    return float(b)/float(a) if float(a)>0 else 1/30


def preview_project_snapshot(path,snapshot):
    """Validate in-memory preview sources with the same folder boundary as disk."""
    if not isinstance(snapshot,dict) or snapshot.get('format')!='katman' or snapshot.get('version')!=1 or not isinstance(snapshot.get('sources'),list):
        raise ValueError('The preview source snapshot is invalid.')
    root=Path(path).parent.resolve();ids=set()
    for source in snapshot['sources']:
        if not isinstance(source,dict) or not isinstance(source.get('id'),str) or not source['id'] or source['id'] in ids or source.get('type') not in ('video','audio') or not isinstance(source.get('file'),str) or not source['file'] or '\0' in source['file']:
            raise ValueError('The preview source snapshot is invalid.')
        ids.add(source['id']);file=(root/source['file']).resolve()
        if not file.is_relative_to(root):raise ValueError('Media files must be inside the project folder.')
        if not file.is_file():raise ValueError('Source media is missing: '+source['file'])
    return copy.deepcopy(snapshot)


def media_fingerprint(path,project,model=None):
    """Invalidate previews when source content or external effect assets change."""
    files=[(Path(path).parent/source['file']).resolve() for source in project['sources']]
    for track in (model or {}).get('tracks',[]):
        for clip in track['clips']:
            files.extend(Path(clip[key]).expanduser().resolve() for key in ('lut','stabilization') if clip.get(key))
    rows=[]
    for file in files:
        stat=file.stat();rows.append((str(file),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
    return json.dumps(rows,separators=(',',':'))



def caption_ass(captions,style,canvas):
    """Canonical ASS used by exports and isolated caption sprites."""
    native_w,native_h=canvas
    def stamp(t):
        cs=round(max(0,t)*100);seconds,cs=divmod(cs,100);minutes,seconds=divmod(seconds,60);hours,minutes=divmod(minutes,60);return f'{hours}:{minutes:02}:{seconds:02}.{cs:02}'
    global_style=style
    styles=[];events=[]
    for i,caption in enumerate(captions):
        style={**global_style,**caption.get('style',{})};font=style.get('font','Noto Sans').replace(',',' ');size=style.get('size',36);color=style.get('color','#ffffff').lstrip('#');ass_color='&H00'+color[4:6]+color[2:4]+color[0:2]
        name='Caption'+str(i);styles.append(f'Style: {name},{font},{size},{ass_color},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,{style.get("margin_left",20)},{style.get("margin_right",20)},{style.get("margin_bottom",20)},1')
        text=caption['text'].replace('\\','\\\\').replace('{','\\{').replace('}','\\}').replace('\r','').replace('\n','\\N')
        events.append(f'Dialogue: 0,{stamp(caption["start"])},{stamp(caption["end"])},{name},,0,0,0,,{text}')
    header=f'[Script Info]\nScriptType: v4.00+\nPlayResX: {native_w}\nPlayResY: {native_h}\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
    return header+'\n'.join(styles)+'\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'+'\n'.join(events)

def compile_graph(folder,project,model,tmp,options=None,window=None,frame=False):
    from .editing import annotation_image
    options=options or {};origin,end=window or (0,duration(model));span=max(.04,end-origin)
    native_w,native_h=model['width'],model['height'];width,height=options.get('size',(native_w,native_h));factor=min(width/native_w,height/native_h)
    cw,ch=max(2,round(native_w*factor/2)*2),max(2,round(native_h*factor/2)*2);fps=options.get('fps',model.get('fps',30))
    sources={s['id']:s for s in project['sources']};args=['ffmpeg','-hide_banner','-v','error','-y','-nostdin','-filter_complex_threads','2'];graph=[] if options.get('audio_only') else [f'color=c=black:s={cw}x{ch}:r={fps}:d={number(span)}[background]'];video='background';audio=[];input_index=0;counter=0
    for track in model['tracks']:
        if options.get('audio_only') and track['kind']=='video' or (frame or options.get('video_only')) and track['kind']=='audio':continue
        if track.get('mute') and track['kind']=='audio' or not track.get('visible',True) and track['kind']=='video':continue
        for clip in track['clips']:
            if clip.get('mute'):continue
            source=sources[clip['source_id']];isvideo=source['type']=='video';file=(folder/source['file']).resolve()
            if not file.is_relative_to(folder.resolve()):raise ValueError('Media must remain inside the recording folder.')
            pieces=rate_segments(clip)
            if clip.get('freeze'):pieces=[(0,clip_duration(clip),0,1)]
            for out_offset,out_length,source_offset,rate in pieces:
                a=max(origin,clip['start']+out_offset);b=min(end,clip['start']+out_offset+out_length)
                if b<=a+.00001:continue
                raw_length=(b-a)*rate;offset=source_offset+(a-clip['start']-out_offset)*rate
                if clip.get('reverse'):offset=clip['length']-offset-(source_frame_interval(str(file),file.stat().st_mtime_ns,file.stat().st_size) if frame else raw_length)
                seek=clip.get('freeze_at',clip['in']) if clip.get('freeze') else clip['in']+offset
                proxy_key=hashlib.sha256((str(file)+str(file.stat().st_mtime_ns)).encode()).hexdigest()[:20];proxy=CACHE/'studio-proxies'/(proxy_key+'.mp4')
                if options.get('proxy') and isvideo and proxy.exists() and not clip.get('stabilization'):file=proxy
                image=file.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.bmp')
                args+=(['-framerate',str(fps),'-loop','1'] if image else ['-ss',number(max(0,seek))])+['-i',str(file)]
                idx=input_index;input_index+=1;tag='c'+str(counter);counter+=1;local=f'(t+{number(origin-clip["start"])})';geq_local=local.replace('t','T');filters=[]
                if isvideo:
                    if frame or clip.get('freeze') or image:filters+=['trim=end_frame=1','setpts=PTS-STARTPTS',f'tpad=stop_mode=clone:stop_duration={number(b-a)}']
                    else:filters+=[f'trim=duration={number(raw_length)}']
                    if clip.get('stabilization'):
                        tr=Path(clip['stabilization'])
                        if not tr.is_file():raise ValueError('Stabilization analysis is missing. Analyze this clip again.')
                        filters+=["format=yuv420p",f"vidstabtransform=input='{quote(tr)}':smoothing=15:optzoom=1"]
                    if clip.get('reverse') and not clip.get('freeze') and not frame:filters+=['reverse']
                    filters+=[f'setpts=(PTS-STARTPTS)/{number(rate)}+{number(a-origin)}/TB']
                    if any(clip.get(key,default)!=default for key,default in [('crop_x',0),('crop_y',0),('crop_w',1),('crop_h',1)]):
                        filters+=[f"crop=w=iw*{number(clip.get('crop_w',1))}:h=ih*{number(clip.get('crop_h',1))}:x=iw*{number(clip.get('crop_x',0))}:y=ih*{number(clip.get('crop_y',0))}"]
                    for key,flt in [('flip_x','hflip'),('flip_y','vflip')]:
                        if clip.get(key):filters.append(flt)
                    brightness=clip.get('brightness',0);contrast=clip.get('contrast',1);saturation=clip.get('saturation',1);gamma=clip.get('gamma',1)
                    if (brightness,contrast,saturation,gamma)!=(0,1,1,1):filters+=[f'eq=brightness={number(brightness)}:contrast={number(contrast)}:saturation={number(saturation)}:gamma={number(gamma)}']
                    if clip.get('temperature') or clip.get('tint'):filters+=[f'colorbalance=rs={number(clip.get("temperature",0))}:rm={number(clip.get("temperature",0))}:rh={number(clip.get("temperature",0))}:bs={number(-clip.get("temperature",0))}:bm={number(-clip.get("temperature",0))}:bh={number(-clip.get("temperature",0))}:gs={number(clip.get("tint",0))}:gm={number(clip.get("tint",0))}:gh={number(clip.get("tint",0))}']
                    if clip.get('sharpness'):filters+=[f'unsharp=5:5:{number(clip["sharpness"])}:5:5:0']
                    if clip.get('lut'):
                        lut=Path(clip['lut'])
                        if not lut.is_file():raise ValueError('The selected LUT file is missing.')
                        filters+=[f"lut3d=file='{quote(lut)}'"]
                    if clip.get('blackout'):filters+=['drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill']
                    w=f'max(2,trunc(({curve(clip,"w",local,source.get("width",cw))})*{number(factor)}/2)*2)';h=f'max(2,trunc(({curve(clip,"h",local,source.get("height",ch))})*{number(factor)}/2)*2)'
                    filters+=['format=rgba',f"scale=w='{w}':h='{h}':eval=frame:flags=bilinear",'setsar=1']
                    if clip.get('border_width',0):filters+=[f'drawbox=x=0:y=0:w=iw:h=ih:color={clip.get("border_color","#ffffff").replace("#","0x")}:t={max(1,round(clip["border_width"]*factor))}']
                    x=f'({curve(clip,"x",local,0)})*{number(factor)}';y=f'({curve(clip,"y",local,0)})*{number(factor)}'
                    rotation=clip.get('rotation',0) or clip.get('keys',{}).get('rotation')
                    if rotation:
                        maxw=max([clip.get('w',cw)]+[v for t,v in clip.get('keys',{}).get('w',[])])*factor;maxh=max([clip.get('h',ch)]+[v for t,v in clip.get('keys',{}).get('h',[])])*factor
                        bound=math.ceil(math.hypot(maxw,maxh)/2)*2;angle=curve(clip,'rotation',local,0)
                        filters+=[f'pad={bound}:{bound}:(ow-iw)/2:(oh-ih)/2:color=black@0:eval=frame',f"rotate=a='({angle})*PI/180':ow=iw:oh=ih:c=none"]
                        x=f'({x})+(({w})-{bound})/2';y=f'({y})+(({h})-{bound})/2'
                    alpha=curve(clip,'opacity',geq_local,1)
                    effect_time=f'({geq_local}+{number(clip.get("effect_offset",0))})'
                    if clip.get('fade_in'):alpha+=f'*min(1,max(0,({effect_time})/{number(clip["fade_in"])}))'
                    if clip.get('fade_out'):alpha+=f'*min(1,max(0,({number(clip.get('effect_length',clip_duration(clip)))}-({effect_time}))/{number(clip["fade_out"])}))'
                    if clip.get('keys',{}).get('opacity') or clip.get('fade_in') or clip.get('fade_out'):
                        filters+=[f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({alpha})'"]
                    elif clip.get('opacity',1)!=1:filters+=[f'colorchannelmixer=aa={number(clip["opacity"])}']
                    graph.append(f'[{idx}:v]'+','.join(filters)+f'[{tag}]');new='v'+str(counter)
                    graph.append(f"[{video}][{tag}]overlay=x='{x}':y='{y}':enable='between(t,{number(a-origin)},{number(b-origin-.00001)})':eof_action=pass:repeatlast=0[{new}]");video=new
                else:
                    filters+=[f'atrim=duration={number(raw_length)}','asetpts=PTS-STARTPTS']
                    if clip.get('reverse'):filters+=['areverse']
                    filters+=[f'atempo={number(math.sqrt(rate))}',f'atempo={number(math.sqrt(rate))}']
                    if clip.get('denoise'):filters+=['afftdn=nf=-25']
                    if clip.get('normalize'):filters+=['loudnorm=I=-16:TP=-1.5:LRA=11']
                    avar=f'(t+{number(a-clip["start"])})';gain=curve(clip,'gain',avar,1)
                    effect_time=f'({avar}+{number(clip.get("effect_offset",0))})'
                    if clip.get('fade_in'):gain+=f'*min(1,max(0,{effect_time}/{number(clip["fade_in"])}))'
                    if clip.get('fade_out'):gain+=f'*min(1,max(0,({number(clip.get('effect_length',clip_duration(clip)))}-{effect_time})/{number(clip["fade_out"])}))'
                    filters+=[f"volume='{gain}':eval=frame",f'adelay={round((a-origin)*1000)}:all=1',f'apad,atrim=duration={number(span)}']
                    graph.append(f'[{idx}:a]'+','.join(filters)+f'[{tag}]');audio.append('['+tag+']')
    for index,ann in enumerate([] if options.get('audio_only') else model.get('annotations',[])):
        if ann['end']<=origin or ann['start']>=end:continue
        kind=ann['kind'];local=f'(t+{number(origin-ann["start"])})';enable=f'between(t,{number(max(0,ann["start"]-origin))},{number(ann["end"]-origin)})';tag='annotation'+str(index)
        if kind in ('redact','blur','pixelate'):
            gvar=local.replace('t','T');x=f'({curve(ann,"x",gvar,0)})*{number(factor)}';y=f'({curve(ann,"y",gvar,0)})*{number(factor)}';w=f'({curve(ann,"w",gvar,100)})*{number(factor)}';h=f'({curve(ann,"h",gvar,100)})*{number(factor)}'
            mask=f'255*between(X,{x},({x})+({w}))*between(Y,{y},({y})+({h}))*between(T,{number(max(0,ann["start"]-origin))},{number(ann["end"]-origin)})'
            graph.append(f"color=black:s={cw}x{ch}:r={fps}:d={number(span)},format=yuv444p,geq=lum='{mask}':cb='{mask}':cr='{mask}'[{tag}mask]")
            graph.append(f'[{video}]format=yuv444p[{tag}formatted]')
            if kind=='redact':graph.append(f'color=black:s={cw}x{ch}:r={fps}:d={number(span)},format=yuv444p[{tag}effect]');base=tag+'formatted'
            else:
                graph.append(f'[{tag}formatted]split[{tag}base][{tag}copy]');base=tag+'base'
                effect='boxblur=12:2' if kind=='blur' else f'scale={max(2,cw//24)}:{max(2,ch//24)}:flags=neighbor,scale={cw}:{ch}:flags=neighbor'
                graph.append(f'[{tag}copy]{effect}[{tag}effect]')
            new=tag+'out';graph.append(f'[{base}][{tag}effect][{tag}mask]maskedmerge[{new}]');video=new
        elif kind=='text':
            text=tmp/(tag+'.txt');text.write_text(ann.get('text',''),encoding='utf8');font=font_file(ann.get('font','Noto Sans'))
            x=f'({curve(ann,"x",local,0)})*{number(factor)}';y=f'({curve(ann,"y",local,0)})*{number(factor)}';alpha=curve(ann,'opacity',local,1)
            graph.append(f"[{video}]drawtext=fontfile='{quote(font)}':textfile='{quote(text)}':fontsize={number(ann.get('font_size',36)*factor)}:fontcolor={ann.get('color','#ffffff').replace('#','0x')}:borderw=1:bordercolor=black:expansion=none:alpha='{alpha}':x='{x}':y='{y}':enable='{enable}'[{tag}]");video=tag
        else:
            overlay=tmp/(tag+'.png');size=(max(2,round(ann.get('w',native_w)*factor)),max(2,round(ann.get('h',native_h)*factor)))
            stroke={**ann,'tool':kind};annotation_image({'annotations':[stroke]},size,overlay);args+=['-framerate',str(fps),'-loop','1','-i',str(overlay)];idx=input_index;input_index+=1
            x=f'({curve(ann,"x",local,0)})*{number(factor)}';y=f'({curve(ann,"y",local,0)})*{number(factor)}'
            aw=f'max(2,({curve(ann,"w",local,native_w)})*{number(factor)})';ah=f'max(2,({curve(ann,"h",local,native_h)})*{number(factor)})'
            filters=f"format=rgba,scale=w='{aw}':h='{ah}':eval=frame"
            if ann.get('keys',{}).get('opacity'):
                alpha=curve(ann,'opacity',local.replace('t','T'),1);filters+=f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({alpha})'"
            elif ann.get('opacity',1)!=1:filters+=f",colorchannelmixer=aa={number(ann['opacity'])}"
            graph.append(f'[{idx}:v]{filters}[{tag}image]')
            graph.append(f"[{video}][{tag}image]overlay=x='{x}':y='{y}':enable='{enable}':eof_action=pass[{tag}]");video=tag
    if model.get('subtitles') and not options.get('audio_only'):
        captions=[{**s,'start':max(0,s['start']-origin),'end':s['end']-origin} for s in model['subtitles'] if s['end']>origin and s['start']<end]
        if captions:
            file=tmp/'captions.ass';file.write_text(caption_ass(captions,model.get('subtitle_style',{}),(native_w,native_h)),encoding='utf8')
            graph.append(f"[{video}]ass=filename='{quote(file)}'[captioned]");video='captioned'
    if not options.get('audio_only'):graph.append(f'[{video}]'+('format=rgba,' if options.get('pixel_format')=='rgba' else '')+f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{"format=nv12,hwupload" if (not frame and options.get("codec","").endswith("_vaapi")) else "format="+options.get("pixel_format","yuv420p")}[vout]')
    if audio:graph.append(''.join(audio)+f'amix=inputs={len(audio)}:normalize=0:duration=longest,alimiter=limit=0.95:latency=1[aout]')
    elif not frame and not options.get('video_only'):graph.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={number(span)}[aout]')
    if (not frame and not options.get('audio_only') and options.get('codec','').endswith('_vaapi')):
        from .hardware import render_device
        device=options.get('vaapi_device') or render_device(settings())
        args[1:1]=['-init_hw_device','vaapi=layer:'+device,'-filter_hw_device','layer']
    script=tmp/'graph.ffscript';script.write_text(';\n'.join(graph));args+=[graph_file_option(),str(script)]
    if not options.get('audio_only'):args+=['-map','[vout]']
    if not frame and not options.get('video_only'):args+=['-map','[aout]']
    args+=['-t',number(span)]
    return args


def export_studio(path,destination,options=None,progress=None,cancel=None,model=None,window=None,frame=False,project_snapshot=None):
    if project_snapshot is None:path,project=load_project(path)
    else:
        path=Path(path).expanduser().resolve()
        if path.is_dir():path=path/'project.katman'
        project=preview_project_snapshot(path,project_snapshot)
    model=copy.deepcopy(model or project['studio']);validate(model,project);options=copy.deepcopy(options or {})
    dest=Path(destination).expanduser().resolve()
    if dest.exists():raise ValueError('Choose a new filename. Existing media is never overwritten.')
    if dest==path or dest in {(path.parent/source['file']).resolve() for source in project['sources']} or dest.is_relative_to(path.parent/'media'):raise ValueError('Exports cannot replace original recording media.')
    dest.parent.mkdir(parents=True,exist_ok=True);ext=dest.suffix or '.mp4';output=dest.with_name('.'+dest.stem+'-'+uuid.uuid4().hex[:8]+ext)
    try:
        with tempfile.TemporaryDirectory(prefix='layer-studio-',dir=dest.parent) as td:
            td=Path(td);args=compile_graph(path.parent,project,model,td,options,window,frame)
            if frame:args+=['-frames:v','1','-threads','1']
            elif options.get('audio_only'):
                codec={'.wav':'pcm_s16le','.mp3':'libmp3lame'}.get(ext.lower(),'aac');args+=['-c:a',codec,'-ar','48000']
                if codec!='pcm_s16le':args+=['-b:a','192k']
            else:
                codec=options.get('codec','libx264');quality=options.get('quality',20)
                args+=['-c:v',codec]
                if codec.endswith('_nvenc'):args+=['-preset','p4','-rc','vbr','-cq',str(quality),'-b:v','0']
                elif codec.endswith('_vaapi'):args+=['-rc_mode','CQP','-qp',str(quality)]
                elif codec=='libaom-av1':args+=['-cpu-used','6','-crf',str(quality),'-b:v','0']
                else:
                    args+=['-preset',options.get('preset','fast'),'-crf',str(quality)]
                    if codec=='libx265':args+=['-x265-params','pools=4:frame-threads=2:log-level=error']
                args+=['-threads','4','-r',str(options.get('fps',model.get('fps',30))),'-c:a','aac','-b:a','192k','-ar','48000']
                if ext.lower() in ('.mp4','.mov'):args+=['-movflags','+faststart']
            args+=['-progress','pipe:1','-stats_period','0.2',str(output)]
            total=(window[1]-window[0]) if window else duration(model)
            run_process(args,td/'error.log',cancel,progress,total)
            os.link(output,dest);output.unlink()
            if progress:progress(1)
            return str(dest)
    finally:output.unlink(missing_ok=True)
