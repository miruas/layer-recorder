#!/usr/bin/env python3
"""Reproducible generated-media preview measurements; never reads user media."""
import argparse,json,subprocess,tempfile,time,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from katman.common import atomic_json
from katman.studio_model import from_project
from katman.studio_render import export_studio
from katman.studio_jobs import preview_options

def fixture(root,seconds=2):
    (root/'media').mkdir();sources=[]
    for i,color in enumerate(('red','blue')):
        name=f'screen{i}';file=f'media/{name}.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color={color}:s=1920x1080:r=24:d={seconds}','-c:v','libx264','-preset','ultrafast','-threads','2',str(root/file)],check=True)
        sources.append(dict(id=name,name=name,type='video',file=file,width=1920,height=1080,logical_width=1920,logical_height=1080,x=1920*i,y=0,media_duration=seconds,timeline_start=0,trim_in=0))
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency=440:duration={seconds}:sample_rate=48000','-c:a','flac',str(root/'media/audio.mkv')],check=True)
    sources.append(dict(id='audio',name='audio',type='audio',file='media/audio.mkv',media_duration=seconds,timeline_start=0,trim_in=0))
    project=dict(format='katman',version=1,duration=seconds,sources=sources,sections=[])
    model=from_project(project);model['tracks'][0]['clips'][0].update(crop_x=.1,crop_w=.9)
    model['annotations']=[dict(id='privacy',kind='redact',start=0,end=seconds,x=200,y=100,w=300,h=200),dict(id='title',kind='text',text='Synthetic preview',start=0,end=seconds,x=20,y=20,font_size=60)]
    project['studio']=model;path=root/'project.katman';atomic_json(path,project);return path,model

def main():
    p=argparse.ArgumentParser();p.add_argument('--stream',action='store_true');p.add_argument('--seconds',type=float,default=2);args=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='layer-preview-bench-') as td:
        root=Path(td);path,model=fixture(root,args.seconds);options=preview_options(model,960);result={}
        for label in ('cold_frame','warm_frame'):
            t=time.monotonic();export_studio(path,root/(label+'.png'),options,model=model,window=(.8,.85),frame=True);result[label]=round(time.monotonic()-t,4)
        t=time.monotonic();export_studio(path,root/'full.mp4',options,model=model);result['full_preencode']=round(time.monotonic()-t,4)
        if args.stream:
            proc=subprocess.Popen([sys.executable,'-m','katman.preview_stream'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            t=time.monotonic();proc.stdin.write(json.dumps(dict(path=str(path),model=model,position=.8,size=options['size'],fps=24,audio=False)).encode());proc.stdin.close()
            count=options['size'][0]*options['size'][1]*4;data=proc.stdout.read(count);result['first_stream_frame']=round(time.monotonic()-t,4);result['first_frame_bytes']=len(data)
            proc.stdout.read();err=proc.stderr.read();proc.wait();assert proc.returncode==0,err.decode()
        print(json.dumps(result,indent=2))
if __name__=='__main__':main()
