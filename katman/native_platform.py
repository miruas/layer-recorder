# SPDX-License-Identifier: GPL-3.0-only
"""Persistent metadata/configuration bridge. Never captures or stores media."""
import json,sys,os
from .common import APP,settings
from .hardware import gsr_program,gsr_info,capture_command
from .platform_capture import displays,hyprland_available,desktop_integrations
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
                elif op in ('plan','plan-record'):
                    cfg=settings('record' if op=='plan-record' else 'replay');program=gsr_program(cfg);info=gsr_info(program)
                    if info.get('layer_audio_node_selector')!='yes':raise RuntimeError('This capture engine needs the Layer GSR fork with independent audio-node support. Rebuild gpu-screen-recorder with tools/build-native.py.')
                    if info.get('layer_segment_muxer')=='no':raise RuntimeError('The GSR build lacks the FFmpeg segment muxer required for Layer replay. Rebuild the complete Layer package.')
                    items=displays(program,cfg['capture_targets'],include_capture_mode=True);direct={m['name'] for m in items}
                    plans=[]
                    for item in items:
                        command,backend=capture_command(item,cfg,direct,info,None)
                        plans.append({'key':'display:'+item['name'],'meta':{'type':'video',**item,'backend':backend},'command':command})
                    from .audio_plan import audio_plan
                    audio=audio_plan(program,info)
                    plans.extend(audio['tracks'])
                    data={'config':cfg,'program':program,'capabilities':info,'tracks':plans,'audio_mode':audio['mode'],'warnings':audio['warnings']}
                elif op=='video-plan':
                    from .display_inventory import display_inventory
                    cfg=request.get('config')
                    if cfg is None:cfg=settings()
                    data=display_inventory(APP,cfg)
                elif op=='identity':data={'application':focused_application(),'shortcut_error':watcher.error if watcher else '', 'integrations':desktop_integrations()}
                else:raise ValueError('Unknown platform operation.')
                reply={'ok':True,'data':data}
            except Exception as e:reply={'ok':False,'error':str(e)}
            print(json.dumps(reply),flush=True)
    finally:
        if watcher:watcher.stop.set()

if __name__=='__main__':main()
