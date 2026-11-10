# SPDX-License-Identifier: GPL-3.0-only
"""GSR capture inventory; compositor geometry is optional enrichment."""
import json,os,re,subprocess

def hyprland_available():
    return bool(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'))

def displays(program,targets=None):
    p=subprocess.run([program,'--list-monitors'],capture_output=True,text=True,timeout=15)
    standalone=lambda target:target in ('portal','focused') or target.isdecimal() or target.startswith('/dev/video')
    if p.returncode and (not targets or not all(standalone(target) for target in targets)):raise RuntimeError('GSR display discovery failed: '+p.stderr[-1000:])
    result=[];x=0
    for line in p.stdout.splitlines():
        match=re.fullmatch(r'([^|]+)\|(\d+)x(\d+)',line.strip())
        if not match:continue
        name,w,h=match.groups();w,h=int(w),int(h)
        result.append(dict(name=name,label=name,x=x,y=0,width=w,height=h,logical_width=w,logical_height=h,layout_provider='synthetic'));x+=w
    if result and hyprland_available():
        try:
            p=subprocess.run(['hyprctl','monitors','-j'],capture_output=True,text=True,timeout=2)
            if p.returncode:raise ValueError('Hyprland geometry unavailable')
            layout={m['name']:m for m in json.loads(p.stdout) if not m.get('disabled')}
            for item in result:
                m=layout.get(item['name'])
                if not m:continue
                w,h=m['width'],m['height']
                if m.get('transform',0)%2:w,h=h,w
                scale=m.get('scale',1);item.update(label=m.get('description') or m['name'],x=m['x'],y=m['y'],width=w,height=h,logical_width=round(w/scale),logical_height=round(h/scale),layout_provider='hyprland')
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):pass
    if targets:
        selected=[]
        for target in targets:
            item=next((m for m in result if m['name']==target),None)
            if item:selected.append(item)
            elif target=='portal':selected.append(dict(name='portal',label='GSR portal selection',x=0,y=0,width=0,height=0,logical_width=0,logical_height=0,layout_provider='portal-selected'))
            elif target=='focused' or target.isdecimal() or target.startswith('/dev/video'):
                selected.append(dict(name=target,label='GSR '+target,x=0,y=0,width=0,height=0,logical_width=0,logical_height=0,layout_provider='capture-source'))
            else:raise RuntimeError('GSR cannot enumerate the selected source: '+target+'. Select portal explicitly for a permission-based capture.')
        result=selected
    if not result:raise RuntimeError('GSR found no capture outputs. Configure capture_targets=["portal"] to request an explicit portal selection.')
    return result
