# SPDX-License-Identifier: GPL-3.0-only
"""Independent clip/track editing, animation and lossless media assembly."""
import copy
import math
import re
import shutil
import uuid
from pathlib import Path


def uid():return uuid.uuid4().hex[:12]


def value_at(item,key,t,default=0):
    points=sorted(item.get('keys',{}).get(key,[]))
    if not points:return float(item.get(key,default))
    if t<=points[0][0]:return float(points[0][1])
    for a,b in zip(points,points[1:]):
        if a[0]<=t<=b[0]:
            f=(t-a[0])/max(.000001,b[0]-a[0]);return a[1]+(b[1]-a[1])*f
    return float(points[-1][1])


def rate_segments(clip):
    length=float(clip.get('length',clip.get('duration',1)*clip.get('speed',1)))
    keys=sorted([[max(0,min(length,float(t))),float(v)] for t,v in clip.get('speed_keys',[])])
    if not keys or keys[0][0]>0:keys.insert(0,[0,float(clip.get('speed',1))])
    pieces=[];out=0
    for i,(a,rate) in enumerate(keys):
        b=keys[i+1][0] if i+1<len(keys) else length
        if b<=a:continue
        pieces.append((out,(b-a)/rate,a,rate));out+=(b-a)/rate
    return pieces


def clip_duration(clip):
    if clip.get('freeze'):return float(clip.get('duration',1))
    return sum(s[1] for s in rate_segments(clip))


def source_offset(clip,t):
    if clip.get('freeze'):return float(clip.get('freeze_at',clip['in']))
    pieces=rate_segments(clip);t=max(0,min(clip_duration(clip),t))
    for start,duration,offset,rate in pieces:
        if t<=start+duration+.000001:
            local=offset+(t-start)*rate
            if clip.get('reverse'):local=clip['length']-local
            return clip['in']+local
    return clip['in']+clip.get('length',0)


def duration(model):return max([0]+[c['start']+clip_duration(c) for tr in model['tracks'] for c in tr['clips']]+[a['end'] for a in model.get('annotations',[])]+[s['end'] for s in model.get('subtitles',[])])


def find_clip(model,id):
    for tr in model['tracks']:
        for clip in tr['clips']:
            if clip['id']==id:return tr,clip
    raise ValueError('Select a timeline clip first.')


def all_clips(model):return [c for tr in model['tracks'] for c in tr['clips']]


def trim_clip(clip,side,delta):
    # Preserve the evaluated animation and fade envelope when removing a range.
    total=clip_duration(clip)
    if (side=='left' and delta>=0) or (side=='right' and delta<=0):
        a=min(delta,total-.04) if side=='left' else 0
        b=total if side=='left' else max(.04,total+delta)
        result=slice_clip(clip,a,b);clip.clear();clip.update(result);return
    if clip.get('freeze'):
        extra=-delta if side=='left' else delta
        if side=='left':extra=min(extra,clip['start']);clip['start']-=extra
        clip['duration']=total+extra;return
    # Split rate segments rather than changing the meaning of animated playback.
    if clip.get('speed_keys'):
        old=clip_duration(clip)
        if side=='left' and delta>=0:result=slice_clip(clip,min(delta,old-.04),old)
        elif side=='right' and delta<=0:result=slice_clip(clip,0,max(.04,old+delta))
        else:
            rate=rate_segments(clip)[0 if side=='left' else -1][3]
            if side=='left':
                extra=min(-delta,clip['in']/rate);result=copy.deepcopy(clip);result['start']=max(0,clip['start']-extra);result['in']-=extra*rate;result['length']+=extra*rate;result['speed_keys']=[[0,rate]]+[[t+extra*rate,v] for t,v in clip['speed_keys']]
            else:result=copy.deepcopy(clip);result['length']+=delta*rate
        clip.clear();clip.update(result);clip['duration']=clip_duration(clip);return
    speed=clip.get('speed',1);old=clip_duration(clip)
    if side=='left':
        delta=max(-clip['in']/speed,min(old-.04,delta));clip['start']=max(0,clip['start']+delta)
        if not clip.get('reverse'):clip['in']+=delta*speed
        clip['length']-=delta*speed
    else:
        delta=max(.04-old,delta);clip['length']+=delta*speed
        if clip.get('reverse'):clip['in']-=delta*speed
    clip['duration']=clip_duration(clip)


def slice_keys(item,start,end):
    result={}
    for key,points in item.get('keys',{}).items():
        rows=[[0,value_at(item,key,start)]]+[[t-start,v] for t,v in points if start<t<end]+[[end-start,value_at(item,key,end)]]
        result[key]=rows
    return result


def slice_clip(clip,start,end):
    """Return a source-preserving slice, including reverse and segmented speeds."""
    result=copy.deepcopy(clip)
    def consumed(t):
        for a,span,offset,rate in rate_segments(clip):
            if t<=a+span+.00001:return offset+max(0,t-a)*rate
        return clip['length']
    first,last=consumed(start),consumed(end);amount=max(.001,last-first)
    result['in']=clip['in']+(clip['length']-last if clip.get('reverse') else first)
    result['length']=amount;result['start']=clip['start']+start
    if clip.get('speed_keys'):
        keys=sorted(clip['speed_keys']);rate=clip.get('speed',1)
        for t,v in keys:
            if t<=first+.00001:rate=v
        result['speed_keys']=[[0,rate]]+[[t-first,v] for t,v in keys if first<t<last]
    result['duration']=end-start
    result['keys']=slice_keys(clip,start,end)
    result['effect_offset']=clip.get('effect_offset',0)+start;result['effect_length']=clip.get('effect_length',clip_duration(clip))
    return result


def split_clip(model,id,position):
    track,clip=find_clip(model,id);local=position-clip['start'];total=clip_duration(clip)
    if not .02<local<total-.02:raise ValueError('Place the playhead inside a selected clip.')
    first=slice_clip(clip,0,local);second=slice_clip(clip,local,total);second['id']=uid()
    index=track['clips'].index(clip);track['clips'][index:index+1]=[first,second];return second['id']


def from_project(project):
    if project.get('studio'):return copy.deepcopy(project['studio'])
    from .export import sections_for
    from .editing import scene_layout,crop_geometry
    sources={s['id']:s for s in project['sources']};sections=sections_for(project)
    sizes=[]
    for sec in sections:
        w,h,_=scene_layout([s for s in project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']],sec)
        sizes.append(crop_geometry(w,h,sec)[2:])
    width=max(w for w,h in sizes);height=max(h for w,h in sizes)
    tracks=[{'id':uid(),'source_id':s['id'],'name':s['name'],'kind':s['type'],'mute':False,'visible':True,'clips':[]} for s in project['sources']]
    model={'version':1,'width':width,'height':height,'fps':int(project.get('fps',60)),'tracks':tracks,'annotations':[],'subtitles':[],'subtitle_style':{'font':'Noto Sans','size':max(20,round(height*.045)),'color':'#ffffff'},'snap':True}
    offset=0
    for sec,(w,h) in zip(sections,sizes):
        speed=sec.get('speed',1);span=sec['end']-sec['start'];_,_,rects=scene_layout([s for s in project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']],sec)
        cw,ch,_=scene_layout([s for s in project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']],sec);cx,cy,_,_=crop_geometry(cw,ch,sec)
        for track in tracks:
            id=track['source_id']
            if id not in sec['enabled_ids']:continue
            source=sources[id];delay=max(0,source.get('timeline_start',0)-sec['start']);local=max(0,sec['start']-source.get('timeline_start',0)+source.get('trim_in',0))
            length=min(span-delay,max(0,source.get('media_duration',span)-local))
            if length<=.001:continue
            clip={'id':uid(),'source_id':id,'name':source['name'],'start':offset+delay/speed,'in':local,'length':length,'duration':length/speed,'speed':speed,'gain':sec.get('gains',{}).get(id,1),'keys':{}}
            if source['type']=='video':
                x,y,sw,sh=rects[id];clip.update(x=x-cx+(width-w)/2,y=y-cy+(height-h)/2,w=sw,h=sh,opacity=1,rotation=0,blackout=id in sec.get('blackout_ids',[]))
            track['clips'].append(clip)
        for stroke in sec.get('annotations',[]):
            model['annotations'].append({**copy.deepcopy(stroke),'id':uid(),'kind':stroke['tool'],'start':offset,'end':offset+span/speed,'x':(width-w)/2,'y':(height-h)/2,'w':w,'h':h})
        offset+=span/speed
    return model


def validate(model,project,allow_empty=False):
    ids={s['id'] for s in project['sources']}
    if not (2<=model['width']<=16384 and 2<=model['height']<=16384 and 1<=model['fps']<=240):raise ValueError('Invalid canvas dimensions or frame rate.')
    for track in model['tracks']:
        for c in track['clips']:
            if c['source_id'] not in ids or c['start']<0 or c['in']<0 or c.get('length',0)<=0 or not .25<=c.get('speed',1)<=4:raise ValueError('Check clip timing and source media.')
            if c.get('crop_x',0)<0 or c.get('crop_y',0)<0 or c.get('crop_w',1)<=0 or c.get('crop_h',1)<=0 or c.get('crop_x',0)+c.get('crop_w',1)>1.00001 or c.get('crop_y',0)+c.get('crop_h',1)>1.00001:raise ValueError('Keep the source crop inside the frame.')
            source=next(s for s in project['sources'] if s['id']==c['source_id'])
            if c['in']+c.get('length',0)>source.get('media_duration',float('inf'))+.1 and not c.get('freeze'):raise ValueError('The clip extends beyond its source media.')
            for t,v in c.get('speed_keys',[]):
                if t<0 or not .25<=v<=4:raise ValueError('Speed segment rates must stay between 0.25× and 4×.')
            for key,points in c.get('keys',{}).items():
                for t,v in points:
                    if t<0 or not math.isfinite(v) or key in ('w','h') and v<2 or key=='opacity' and not 0<=v<=1 or key=='gain' and not 0<=v<=4:raise ValueError('Invalid keyframe value or range.')
    if duration(model)<=0 and not allow_empty:raise ValueError('Add a clip or title before exporting.')


def import_sources(path,project,model,source_path,at=0):
    from .common import load_project,probe
    source_path=Path(source_path).resolve();target=Path(path).parent;token=uid()
    if source_path.suffix=='.katman':
        other_path,other=load_project(source_path);other_model=from_project(other);validate(other_model,other)
        mapping={s['id']:token+'-'+str(i) for i,s in enumerate(other['sources'])};files={}
        factor=min(model['width']/other_model['width'],model['height']/other_model['height']);pad_x=(model['width']-other_model['width']*factor)/2;pad_y=(model['height']-other_model['height']*factor)/2
        copied=[]
        try:
            for i,source in enumerate(other['sources']):
                original=other_path.parent/source['file'];dest=target/'media'/(token+'-'+str(i)+original.suffix);shutil.copyfile(original,dest);copied.append(dest);files[source['id']]=str(dest.relative_to(target))
            for source in other['sources']:
                item=copy.deepcopy(source);item['id']=mapping[source['id']];item['file']=files[source['id']];project['sources'].append(item)
            for track in other_model['tracks']:
                track=copy.deepcopy(track);track['id']=uid();track['source_id']=mapping.get(track.get('source_id'),track.get('source_id'))
                track['recording']=other.get('name',source_path.parent.name)+' · '+other.get('created',source_path.parent.name)
                for c in track['clips']:
                    c.update(id=uid(),source_id=mapping[c['source_id']],start=c['start']+at)
                    if track['kind']=='video':
                        for key,offset in [('x',pad_x),('y',pad_y),('w',0),('h',0)]:
                            c[key]=c.get(key,0)*factor+offset
                            if key in c.get('keys',{}):c['keys'][key]=[[t,value*factor+offset] for t,value in c['keys'][key]]
                        if c.get('border_width'):c['border_width']*=factor
                model['tracks'].append(track)
            for field in ('annotations','subtitles'):
                for item in other_model.get(field,[]):
                    item=copy.deepcopy(item);item.update(id=uid(),start=item['start']+at,end=item['end']+at)
                    if field=='annotations':
                        for key,offset in [('x',pad_x),('y',pad_y),('w',0),('h',0)]:
                            item[key]=item.get(key,0)*factor+offset
                            if key in item.get('keys',{}):item['keys'][key]=[[t,value*factor+offset] for t,value in item['keys'][key]]
                        if item.get('font_size'):item['font_size']*=factor
                    else:
                        style=other_model.get('subtitle_style',{});item['style']={**style,**item.get('style',{}),'size':item.get('style',{}).get('size',style.get('size',36))*factor,'margin_left':pad_x+20*factor,'margin_right':pad_x+20*factor,'margin_bottom':pad_y+20*factor}
                    model[field].append(item)
        except BaseException:
            for file in copied:file.unlink(missing_ok=True)
            raise
    else:
        dest=target/'media'/(token+source_path.suffix);shutil.copyfile(source_path,dest);info=probe(dest)
        for stream in info['streams']:
            kind=stream['codec_type']
            if kind not in ('video','audio') or any(s['id']==token+'-'+kind for s in project['sources']):continue
            id=token+'-'+kind;length=float(info.get('format',{}).get('duration',5));source={'id':id,'name':source_path.stem,'type':kind,'file':str(dest.relative_to(target)),'media_duration':length,'trim_in':0,'timeline_start':0}
            clip={'id':uid(),'source_id':id,'name':source['name'],'start':at,'in':0,'length':length,'duration':length,'speed':1,'gain':1,'keys':{}}
            if kind=='video':
                source.update(width=stream['width'],height=stream['height']);factor=min(model['width']/stream['width'],model['height']/stream['height']);w=stream['width']*factor;h=stream['height']*factor;clip.update(x=(model['width']-w)/2,y=(model['height']-h)/2,w=w,h=h,opacity=1)
            project['sources'].append(source);model['tracks'].append({'id':uid(),'source_id':id,'name':source['name'],'kind':kind,'visible':True,'mute':False,'clips':[clip]})


def parse_srt(text):
    def stamp(value):
        h,m,s=value.replace(',','.').split(':');return int(h)*3600+int(m)*60+float(s)
    result=[]
    for block in re.split(r'\n\s*\n',text.replace('\r','').strip()):
        match=re.search(r'(\d{1,2}:\d\d:\d\d[,.]\d+)\s*-->\s*(\d{1,2}:\d\d:\d\d[,.]\d+)[^\n]*\n(.*)',block,re.S)
        if match:
            a,b=stamp(match[1]),stamp(match[2])
            if b>a:result.append({'id':uid(),'start':a,'end':b,'text':match[3].strip()})
    return result


def write_srt(captions):
    def stamp(t):
        ms=round(t*1000);s,ms=divmod(ms,1000);m,s=divmod(s,60);h,m=divmod(m,60);return f'{h:02}:{m:02}:{s:02},{ms:03}'
    return '\n\n'.join(f'{i+1}\n{stamp(c["start"])} --> {stamp(c["end"])}\n{c["text"]}' for i,c in enumerate(captions))+'\n'
