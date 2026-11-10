import copy
import os
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
import test_studio
from katman.common import atomic_json,run,probe
from katman.studio_model import from_project,import_sources
from katman.studio_render import export_studio
from katman.studio_jobs import build_proxies,stabilize,transcribe,track_region

class StudioFeatures(unittest.TestCase):
    setUp=test_studio.StudioRender.setUp
    tearDown=test_studio.StudioRender.tearDown
    render=test_studio.StudioRender.render
    pixel=test_studio.StudioRender.pixel
    def test_resize_animation_rotation_and_opacity(self):
        self.model=from_project(self.project);c=self.model['tracks'][0]['clips'][0]
        c['keys']={'w':[[0,160],[2,80]],'h':[[0,90],[2,45]]}
        file,info=self.render('resize.mp4');self.assertLess(max(self.pixel(file,130,20,1.5)),25)
        c['keys']={'opacity':[[0,1],[2,0]]};file,info=self.render('alpha.mp4');r=self.pixel(file,20,20,1.5)[0];self.assertTrue(35<r<100,r)
        c['keys']={};c['rotation']=90;file,info=self.render('rotated.mp4');self.assertLess(max(self.pixel(file,5,20)),25);self.assertGreater(self.pixel(file,80,20)[0],180)
    def test_lut_and_color_adjustment_and_pixelation(self):
        self.model=from_project(self.project);cube=self.root/'invert.cube';cube.write_text('LUT_3D_SIZE 2\n'+'\n'.join(f'{1-r} {1-g} {1-b}' for b in (0,1) for g in (0,1) for r in (0,1)))
        c=self.model['tracks'][0]['clips'][0];c.update(lut=str(cube),brightness=.02,contrast=1.1,saturation=.8,temperature=.01,sharpness=.2)
        file,info=self.render('graded.mp4');p=self.pixel(file,20,20);self.assertGreater(p[1],150);self.assertGreater(p[2],150);self.assertLess(p[0],90)
        c.pop('lut');c.update(brightness=0,contrast=1,saturation=1,temperature=0,sharpness=0)
        self.model['annotations']=[{'id':'pix','kind':'pixelate','start':0,'end':2,'x':140,'y':0,'w':100,'h':80}]
        file,info=self.render('pixelate.mp4');self.assertGreater(self.pixel(file,159,20)[2],150)
    def test_reverse_and_freeze_use_correct_source_frame(self):
        colors=self.root/'media/colors.mkv'
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=160x90:r=30:d=1','-f','lavfi','-i','color=blue:s=160x90:r=30:d=1','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0','-c:v','libx264',colors])
        self.project['sources'][0]['file']='media/colors.mkv';self.model=from_project(self.project);c=self.model['tracks'][0]['clips'][0];c['reverse']=True
        file,info=self.render('reverse.mp4');self.assertGreater(self.pixel(file,20,20,.2)[2],180);self.assertGreater(self.pixel(file,20,20,1.7)[0],180)
        c.update(reverse=False,freeze=True,freeze_at=1.5,duration=2)
        file,info=self.render('freeze.mp4');self.assertGreater(self.pixel(file,20,20,.2)[2],180);self.assertGreater(self.pixel(file,20,20,1.7)[2],180)
    def test_overlay_keeps_every_video_and_audio_source_independent(self):
        self.model=from_project(self.project);atomic_json(self.path,self.project)
        other=self.root/'other';other.mkdir();(other/'media').mkdir()
        import shutil
        for source in self.project['sources']:shutil.copy2(self.root/source['file'],other/source['file'])
        op=other/'project.katman';atomic_json(op,self.project)
        import_sources(self.path,self.project,self.model,op,.5)
        self.assertEqual(len(self.project['sources']),8);self.assertEqual(len(self.model['tracks']),8)
        self.assertEqual(sum(tr['kind']=='audio' for tr in self.model['tracks']),4)
        self.assertEqual(len({s['id'] for s in self.project['sources']}),8)
        file,info=self.render('independent-overlay.mp4');self.assertAlmostEqual(float(info['format']['duration']),2.5,delta=.1)
    def test_source_proxy_and_stabilization_and_audio_only(self):
        self.model=from_project(self.project);atomic_json(self.path,self.project)
        self.assertEqual(build_proxies(self.path),2)
        c=self.model['tracks'][0]['clips'][0];file=stabilize(self.path,c);self.assertTrue(Path(file).is_file());c['stabilization']=file
        result,info=self.render('stable.mp4');self.assertAlmostEqual(float(info['format']['duration']),2,delta=.1)
        result,info=self.render('audio.m4a',{'audio_only':True});self.assertTrue(all(s['codec_type']=='audio' for s in info['streams']))
    def test_cpu_av1_and_nvidia_export(self):
        self.model=from_project(self.project)
        file,info=self.render('cpu-av1.mkv',{'codec':'libaom-av1','quality':35});self.assertEqual(info['streams'][0]['codec_name'],'av1')
        if os.environ.get('LAYER_TEST_GPU')!='1':return
        file,info=self.render('gpu.mp4',{'codec':'h264_nvenc'});self.assertEqual(info['streams'][0]['codec_name'],'h264')

    @unittest.skipUnless(os.environ.get("LAYER_TEST_GPU")=="1","Set LAYER_TEST_GPU=1 for NVIDIA hardware checks.")
    def test_all_supplied_video_encoder_choices(self):
        self.model=from_project(self.project)
        for codec,expected in [('libx265','hevc'),('hevc_nvenc','hevc'),('av1_nvenc','av1')]:
            file,info=self.render(codec+'.mkv',{'codec':codec,'quality':30});self.assertEqual(info['streams'][0]['codec_name'],expected)
