#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""GPU preview regressions in an existing X11/Wayland test desktop.

Uses generated media and isolated app/config/cache/IPC directories only. Never
captures a display, opens user media, starts a recorder or sends GSR controls.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PIL import Image, ImageChops, ImageStat


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--quick',action='store_true')
    args=parser.parse_args();binary=args.binary.resolve();app_root=args.root.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    results=[]
    with tempfile.TemporaryDirectory(prefix='layer-gpu-test-') as temporary:
        root=Path(temporary);media=root/'media';media.mkdir();runtime=root/'runtime';runtime.mkdir(mode=0o700)
        env=os.environ.copy();env.update(XDG_RUNTIME_DIR=str(runtime),XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),LAYER_CONFIG=str(root/'settings.json'))
        if env.get('WAYLAND_DISPLAY') and not os.path.isabs(env['WAYLAND_DISPLAY']):env['WAYLAND_DISPLAY']=str(Path(os.environ['XDG_RUNTIME_DIR'])/env['WAYLAND_DISPLAY'])
        os.environ.update({key:env[key] for key in ('XDG_RUNTIME_DIR','XDG_CONFIG_HOME','XDG_CACHE_HOME','LAYER_CONFIG')})
        from katman.studio_model import from_project
        from katman.studio_render import export_studio
        Path(env['LAYER_CONFIG']).write_text(json.dumps({'library':str(root/'recordings'),'gsr_preferences':False,'preview_resolution':'native','preview_fps':'source'}))
        for index,(width,height,fps) in enumerate([(640,360,60),(360,640,120)]):
            subprocess.run(['ffmpeg','-hide_banner','-v','error','-f','lavfi','-i',f'testsrc2=s={width}x{height}:r={fps}:d=1','-c:v','ffv1','-threads','2',str(media/f'{index}.mkv')],check=True)
        project={'format':'katman','version':1,'duration':1,'fps':120,'sections':[],'sources':[
            {'id':'a','name':'Landscape','type':'video','file':'media/0.mkv','width':640,'height':360,'x':0,'y':80,'fps':60,'media_duration':1,'timeline_start':0,'trim_in':0},
            {'id':'b','name':'Portrait','type':'video','file':'media/1.mkv','width':360,'height':640,'x':640,'y':0,'fps':120,'media_duration':1,'timeline_start':0,'trim_in':0}]}
        base=from_project(project)
        marks=[{'id':'blur','kind':'blur','start':0,'end':1,'x':420,'y':130,'w':300,'h':230},
               {'id':'pixelate','kind':'pixelate','start':0,'end':1,'x':500,'y':190,'w':280,'h':240},
               {'id':'black','kind':'redact','start':0,'end':1,'x':550,'y':250,'w':110,'h':100},
               {'id':'text','kind':'text','start':0,'end':1,'x':70,'y':545,'w':780,'h':60,'text':'Independent GPU overlays','font':'Noto Sans','font_size':30,'color':'#d7edff','opacity':1}]
        subprocess.run(['ffmpeg','-hide_banner','-v','error','-f','lavfi','-i','nullsrc=s=1000x640:r=60:d=1','-vf',"geq=lum='255*gte(X,500)':cb=128:cr=128",'-c:v','ffv1','-pix_fmt','gray','-threads','2',str(media/'phase.mkv')],check=True)
        cases={'clean':[],'blur':[marks[0]],'pixelate':[marks[1]],'title':[marks[3]],'ordered':marks,'complex-underlay':marks,'pixelate-phase':[{**marks[1],'x':0,'y':0,'w':1000,'h':640}]}
        if not args.quick:cases.update({'redact':[marks[2]],'reversed':list(reversed(marks)),'edge-blur':[{**marks[0],'x':0,'y':0,'w':80,'h':80}]})
        for name,annotations in cases.items():
            p=copy.deepcopy(project);m=copy.deepcopy(base);m['annotations']=copy.deepcopy(annotations)
            if name=='complex-underlay':m['tracks'][0]['clips'][0]['brightness']=.12
            if name=='pixelate-phase':
                p.update(fps=60,sources=[{**project['sources'][0],'id':'phase','file':'media/phase.mkv','width':1000,'height':640,'x':0,'y':0}]);m=from_project(p);m['annotations']=copy.deepcopy(annotations)

            p['studio']=m;path=root/(name+'.katman');path.write_text(json.dumps(p));gpu=out/(name+'-gpu.png');gpu.unlink(missing_ok=True)
            animate=name in ('blur','ordered','complex-underlay')
            command=[str(binary),'--root',str(app_root),'--project',str(path),'--qa-preview',str(gpu),'--qa-position','.5','--qa-after','.7','--qa-size','950x740']
            if animate:command+=['--qa-overlay-edits']
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30);log=result.stdout+result.stderr;(out/(name+'.log')).write_text(log)
            assert result.returncode==0 and 'LAYER_GPU_CAPTURE_OK' in log and gpu.is_file(),name+': '+log[-3000:]
            golden=out/(name+'-reference.png');golden.unlink(missing_ok=True)
            export_studio(path,golden,{'size':[m['width'],m['height']]},model=m,window=(.5,.5+1/120),frame=True)
            with Image.open(gpu) as gi,Image.open(golden) as ri:
                assert gi.size==ri.size==(1000,640),(name,gi.size,ri.size)
                diff=ImageChops.difference(gi.convert('RGB'),ri.convert('RGB'));mae=sum(ImageStat.Stat(diff).mean)/3
                if name=='pixelate-phase':
                    assert gi.getpixel((500,200))[0]<4 and gi.getpixel((520,200))[0]>250,'Pixelation grid does not match fixed-point nearest sampling'

                assert mae<4,(name,'visual mean absolute error',mae)
                if name in ('redact','ordered','complex-underlay'):
                    assert max(channel[1] for channel in gi.convert('RGB').crop((550,250,661,351)).getextrema())==0,(name,'privacy boundary is not opaque')
            metrics={k:int(v) for k,v in re.findall(r'(source_decodes|base_renders|underlay_renders|overlay_edits)=(\d+)',log)}
            if animate:
                assert metrics['overlay_edits']==60,metrics
                assert metrics['base_renders']==1,metrics
                if name=='complex-underlay':assert metrics['underlay_renders']==1,metrics
                else:assert metrics['source_decodes']==2 and metrics['underlay_renders']==0,metrics
            row={'case':name,'passed':True,'mean_absolute_rgb_error':round(mae,3),**metrics};results.append(row);print(json.dumps(row),flush=True)
        # One-pixel luminance detail must survive a native1080 preview. A960px
        # intermediate would average these alternating columns into grey.
        detail=media/'detail.mkv'
        subprocess.run(['ffmpeg','-hide_banner','-v','error','-f','lavfi','-i','nullsrc=s=1920x1080:r=60:d=1','-vf',"geq=lum='255*mod(X,2)':cb=128:cr=128",'-c:v','ffv1','-pix_fmt','gray','-threads','2',str(detail)],check=True)
        p=copy.deepcopy(project);p.update(fps=60,sources=[{**project['sources'][0],'file':'media/detail.mkv','width':1920,'height':1080,'x':0,'y':0,'fps':60}]);p['studio']=from_project(p)
        path=root/'detail.katman';path.write_text(json.dumps(p));gpu=out/'detail1080-gpu.png';gpu.unlink(missing_ok=True)
        result=subprocess.run([str(binary),'--root',str(app_root),'--project',str(path),'--qa-preview',str(gpu),'--qa-position','.5','--qa-after','.7'],env=env,capture_output=True,text=True,timeout=30);(out/'detail1080.log').write_text(result.stdout+result.stderr)
        assert result.returncode==0 and gpu.is_file(),result.stderr
        with Image.open(gpu) as image:
            assert image.size==(1920,1080),image.size
            scan=[image.getpixel((x,500))[0] for x in range(100,200)]
            assert max(scan)-min(scan)>240 and sum(abs(a-b) for a,b in zip(scan,scan[1:]))/99>230,'Native detail was downsampled'
        results.append({'case':'native1080-one-pixel-detail','passed':True,'size':[1920,1080]})
    (out/'report.json').write_text(json.dumps({'results':results},indent=2)+'\n');return 0

if __name__=='__main__':raise SystemExit(main())
