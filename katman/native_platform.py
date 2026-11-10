# SPDX-License-Identifier: GPL-3.0-only
"""Persistent metadata/configuration bridge. Never captures or stores media."""
import json,sys,os
from .common import APP,settings
from .hardware import gsr_program,gsr_info,capture_command
from .platform_capture import displays,hyprland_available
from .identity import focused_application

def main():
    watcher=None
    if hyprland_available():
        from .shortcuts import Watcher
        watcher=Watcher()
    try:
        for raw in sys.stdin:
            try:
                request=json.loads(raw);op=request['op']
                if op=='settings':data=settings()
                elif op=='plan':
                    cfg=settings();program=gsr_program(cfg);info=gsr_info(program)
                    if info.get('layer_audio_node_selector')!='yes':raise RuntimeError('This capture engine needs the Layer GSR fork with independent audio-node support. Rebuild gpu-screen-recorder with tools/build-native.py.')
                    if info.get('supports_app_audio')!='yes':raise RuntimeError('GSR cannot connect to PipeWire application audio. Check the PipeWire PulseAudio service and PULSE_SERVER before capture.')
                    items=displays(program,cfg['capture_targets']);direct={m['name'] for m in items}
                    plans=[]
                    for item in items:
                        command,backend=capture_command(item,cfg,direct,info,None)
                        plans.append({'key':'display:'+item['name'],'meta':{'type':'video',**item,'backend':backend},'command':command})
                    data={'config':cfg,'program':program,'capabilities':info,'tracks':plans}
                elif op=='identity':data={'application':focused_application(),'shortcut_error':watcher.error if watcher else '', 'integrations':{'geometry':'hyprland' if hyprland_available() else 'synthetic','shortcuts':'hyprland' if watcher else 'external','identity':'hyprland' if hyprland_available() else 'desktop'}}
                else:raise ValueError('Unknown platform operation.')
                reply={'ok':True,'data':data}
            except Exception as e:reply={'ok':False,'error':str(e)}
            print(json.dumps(reply),flush=True)
    finally:
        if watcher:watcher.stop.set()

if __name__=='__main__':main()
