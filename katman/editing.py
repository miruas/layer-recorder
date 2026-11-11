# SPDX-License-Identifier: GPL-3.0-only
"""Non-destructive clip geometry, annotations, and recording assembly."""
import copy
import math
import shutil
import tempfile
import uuid
from pathlib import Path


def crop_geometry(w, h, section):
    x, y, width, height = section.get('crop', [0, 0, 1, 1])
    left, top = int(w*x)//2*2, int(h*y)//2*2
    width = max(2, min(int(w*width)//2*2, int(w-left)//2*2))
    height = max(2, min(int(h*height)//2*2, int(h-top)//2*2))
    return left, top, width, height


def scene_layout(videos, section):
    from .export import layout
    if not videos:
        return 1920, 1080, {}
    w, h, default = layout(videos)
    order=section.get('layer_order', [s['id'] for s in videos])
    videos=sorted(videos,key=lambda s:order.index(s['id']) if s['id'] in order else len(order))
    positions = section.get('placements', {})
    rects = {s['id']: tuple(positions.get(s['id'], default[s['id']])) for s in videos}
    left = min(r[0] for r in rects.values())
    top = min(r[1] for r in rects.values())
    rects = {id: (x-left, y-top, sw, sh) for id,(x,y,sw,sh) in rects.items()}
    return math.ceil(max(x+sw for x,y,sw,sh in rects.values())/2)*2, math.ceil(max(y+sh for x,y,sw,sh in rects.values())/2)*2, rects


def scene_size(project, section):
    w,h,_=scene_layout([s for s in project['sources'] if s['type']=='video' and s['id'] in section['enabled_ids']], section)
    return crop_geometry(w,h,section)[2:]


def validate_effects(section, source_ids):
    crop=section.get('crop',[0,0,1,1])
    if len(crop)!=4 or not all(math.isfinite(float(v)) for v in crop):
        raise ValueError('Invalid frame crop.')
    x,y,w,h=crop
    if x<0 or y<0 or w<=0 or h<=0 or x+w>1.000001 or y+h>1.000001:
        raise ValueError('The crop must stay inside the frame.')
    if not .25<=float(section.get('speed',1))<=4:
        raise ValueError('Playback speed must be between 0.25× and 4×.')
    for id,rect in section.get('placements',{}).items():
        if id not in source_ids or len(rect)!=4 or not all(math.isfinite(float(v)) and abs(v)<=32768 for v in rect) or min(rect[2:])<2:
            raise ValueError('Invalid display placement.')
    if not set(section.get('blackout_ids',[])).issubset(source_ids):
        raise ValueError('A blacked-out source is missing.')
    for stroke in section.get('annotations',[]):
        if stroke.get('tool') not in ('pen','line','arrow','rectangle','redact') or len(stroke.get('points',[]))>5000:
            raise ValueError('Invalid drawing tool or path.')
        import re
        if not re.fullmatch(r'#[0-9a-fA-F]{6}',stroke.get('color','#ff4c4c')) or not 0<float(stroke.get('width',.004))<=.1:
            raise ValueError('Invalid drawing color or width.')
        if not stroke.get('points') or any(len(p)!=2 or not all(math.isfinite(float(v)) and 0<=v<=1 for v in p) for p in stroke['points']):
            raise ValueError('Drawing points must stay inside the frame.')


def annotation_image(section, size, destination):
    # Headless rendering: no Qt runtime is loaded by the Rust editor's jobs.
    from PIL import Image,ImageDraw
    w,h=map(int,size);factor=2
    image=Image.new('RGBA',(w*factor,h*factor),(0,0,0,0));draw=ImageDraw.Draw(image)
    for stroke in section.get('annotations',[]):
        points=[(round(x*w*factor),round(y*h*factor)) for x,y in stroke.get('points',[])]
        if not points:continue
        a,b=points[0],points[-1];tool=stroke['tool'];color=stroke.get('color','#ff4c4c');width=max(1,round(stroke.get('width',.004)*h*factor))
        bounds=(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))
        if tool=='redact':draw.rectangle(bounds,fill='#000000')
        elif tool=='rectangle':draw.rectangle(bounds,outline=color,width=width)
        elif tool in ('line','arrow'):
            draw.line([a,b],fill=color,width=width)
            if tool=='arrow':
                angle=math.atan2(b[1]-a[1],b[0]-a[0]);length=max(10*factor,width*4)
                for offset in (-.5,.5):draw.line([b,(b[0]-length*math.cos(angle+offset),b[1]-length*math.sin(angle+offset))],fill=color,width=width)
        else:
            if len(points)>1:draw.line(points,fill=color,width=width,joint='curve')
            radius=width/2
            for x,y in (points if len(points)==1 else (points[0],points[-1])):draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=color)
    image.resize((w,h),Image.Resampling.LANCZOS).save(destination,'PNG')


def join_next(project, index):
    sections=project['sections']
    if index+1>=len(sections):raise ValueError('Select a clip with a following clip to join.')
    a,b=sections[index:index+2]
    if abs(a['end']-b['start'])>.01 or {k:v for k,v in a.items() if k not in ('start','end')}!={k:v for k,v in b.items() if k not in ('start','end')}:
        raise ValueError('Join requires touching clips with matching sources and effects. Different clips already play and export in sequence.')
    a['end']=b['end'];sections.pop(index+1)


def import_recording(project_path, project, source_path, overlay=False, section_index=0):
    from .common import load_project, probe
    from .export import sections_for, export_project
    target=Path(project_path).parent;source_path=Path(source_path).resolve()
    if source_path==Path(project_path).resolve():raise ValueError('Choose a different recording to import.')
    result=copy.deepcopy(project);token=uuid.uuid4().hex[:12];created=[]
    try:
        if source_path.suffix=='.katman' and not overlay:
            parsed,other=load_project(source_path)
            from .export import validate
            validate(other)
            offset=result['duration'];ids={s['id']:token+'-'+s['id'] for s in other['sources']}
            for source_index,source in enumerate(other['sources']):
                item=copy.deepcopy(source);dest=target/'media'/(token+'-'+str(source_index)+Path(source['file']).suffix)
                shutil.copyfile(parsed.parent/source['file'],dest);created.append(dest)
                item.update(id=ids[source['id']],file=str(dest.relative_to(target)),timeline_start=offset+source.get('timeline_start',0))
                result['sources'].append(item)
            for sec in sections_for(other):
                item=copy.deepcopy(sec);item.update(start=sec['start']+offset,end=sec['end']+offset,enabled_ids=[ids[id] for id in sec['enabled_ids']])
                item['gains']={ids[id]:v for id,v in sec.get('gains',{}).items()}
                item['placements']={ids[id]:v for id,v in sec.get('placements',{}).items()}
                item['blackout_ids']=[ids[id] for id in sec.get('blackout_ids',[])]
                item['layer_order']=[ids[id] for id in sec.get('layer_order',[])]
                result['sections'].append(item)
            result['duration']+=other['duration']
        else:
            dest=target/'media'/(token+'.mp4' if source_path.suffix=='.katman' else token+source_path.suffix.lower())
            if source_path.suffix=='.katman':
                with tempfile.TemporaryDirectory(prefix='layer-import-') as td:
                    flattened=Path(td)/'recording.mp4';export_project(source_path,flattened);shutil.copyfile(flattened,dest)
            else:shutil.copyfile(source_path,dest)
            created.append(dest);info=probe(dest);duration=float(info['format']['duration'])
            offset=result['sections'][section_index]['start'] if overlay else result['duration']
            enabled=[]
            for stream in info['streams']:
                if stream['codec_type'] not in ('video','audio'):continue
                kind=stream['codec_type']
                if any(s.endswith('-'+kind) for s in enabled):continue
                id=token+'-'+kind;enabled.append(id)
                item={'id':id,'name':source_path.stem,'type':kind,'file':str(dest.relative_to(target)), 'timeline_start':offset,'trim_in':0,'media_duration':duration,'enabled':True,'gain':1}
                if kind=='video':item.update(width=int(stream['width']),height=int(stream['height']),x=0,y=0)
                result['sources'].append(item)
            if not any(s.endswith('-video') for s in enabled):raise ValueError('Choose a recording with a video stream.')
            if overlay:
                sec=result['sections'][section_index]
                w,h,_=scene_layout([s for s in project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']],sec)
                sec['enabled_ids']+=enabled
                video=next(s for s in result['sources'] if s['id']==token+'-video')
                width=w*.45;height=width*video['height']/video['width']
                sec.setdefault('placements',{})[video['id']]=[(w-width)/2,(h-height)/2,width,height]
            else:
                result['sections'].append({'start':offset,'end':offset+duration,'enabled_ids':enabled,'gains':{id:1 for id in enabled}})
            result['duration']=max(result['duration'],offset+duration)
        return result
    except BaseException:
        for p in created:p.unlink(missing_ok=True)
        raise


def stroke_hit(stroke, point, size):
    """Hit the visible path in canvas pixels, including sparse paths/arrowheads."""
    width,height=size
    points=[(x*width,y*height) for x,y in stroke['points']]
    if not points:return False
    px,py=point[0]*width,point[1]*height
    a,b=points[0],points[-1]
    if stroke['tool'] in ('rectangle','redact'):
        return min(a[0],b[0])<=px<=max(a[0],b[0]) and min(a[1],b[1])<=py<=max(a[1],b[1])
    tolerance=max(7,stroke.get('width',.004)*height/2+5)
    segments=list(zip(points,points[1:])) or [(a,a)]
    if stroke['tool']=='arrow':
        angle=math.atan2(b[1]-a[1],b[0]-a[0]);length=max(10,stroke.get('width',.004)*height*4)
        for offset in (-.5,.5):segments.append((b,(b[0]-length*math.cos(angle+offset),b[1]-length*math.sin(angle+offset))))
    for (ax,ay),(bx,by) in segments:
        dx,dy=bx-ax,by-ay;length2=dx*dx+dy*dy
        t=max(0,min(1,((px-ax)*dx+(py-ay)*dy)/length2)) if length2 else 0
        if math.hypot(px-(ax+t*dx),py-(ay+t*dy))<=tolerance:return True
    return False
