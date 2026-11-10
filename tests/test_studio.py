import copy
import hashlib
import json
import subprocess
import unittest
import test_export
from katman.common import atomic_json,probe
from katman.studio_model import from_project,value_at,clip_duration,split_clip,parse_srt,write_srt
from katman.studio_render import export_studio

class StudioRender(unittest.TestCase):
    setUp=test_export.ExportIntegration.setUp
    def tearDown(self):
        for name,digest in self.hashes.items():self.assertEqual(hashlib.sha256((self.root/'media'/name).read_bytes()).digest(),digest)
        self.temp.cleanup()
    def render(self,name='studio.mp4',options=None):
        self.project['studio']=self.model;atomic_json(self.path,self.project);dest=self.root/name
        export_studio(self.path,dest,options);return dest,probe(dest)
    def pixel(self,file,x,y,t=.5):
        info=probe(file);w=next(s for s in info['streams'] if s['codec_type']=='video')['width']
        data=subprocess.check_output(['ffmpeg','-v','error','-ss',str(t),'-i',str(file),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'])
        return tuple(data[(y*w+x)*3:(y*w+x)*3+3])
    def test_independent_sources_and_keyframed_motion(self):
        self.model=from_project(self.project);left=self.model['tracks'][0]['clips'][0];left['keys']={'x':[[0,0],[2,160]]}
        file,info=self.render();self.assertGreater(self.pixel(file,20,20,.1)[0],180)
        self.assertLess(max(self.pixel(file,20,20,1.5)),25)
        self.assertGreater(self.pixel(file,200,20,1.5)[2],180)
    def test_timed_and_animated_privacy(self):
        self.model=from_project(self.project);self.model['annotations']=[{'id':'mask','kind':'redact','start':.4,'end':1.5,'x':10,'y':10,'w':40,'h':30,'keys':{'x':[[0,10],[1,90]]}}]
        file,info=self.render();self.assertGreater(self.pixel(file,20,20,.1)[0],180)
        self.assertLess(max(self.pixel(file,30,20,.5)),20)
        self.assertGreater(self.pixel(file,30,20,1.3)[0],180)
    def test_text_captions_audio_and_reverse_speed(self):
        self.model=from_project(self.project);self.model['subtitles']=parse_srt('1\n00:00:00,100 --> 00:00:01,500\nHello\n')
        self.model['subtitle_style']['size']=20
        self.model['annotations']=[{'id':'title','kind':'text','text':'Layer','font':'Noto Sans','font_size':24,'color':'#ffffff','start':0,'end':2,'x':5,'y':5}]
        self.model['tracks'][0]['clips'][0]['reverse']=True
        self.model['tracks'][2]['clips'][0].update(denoise=True,normalize=True,fade_in=.1,fade_out=.1)
        file,info=self.render();self.assertTrue(any(s['codec_type']=='audio' for s in info['streams']))
        self.assertEqual(parse_srt(write_srt(self.model['subtitles']))[0]['text'],'Hello')
    def test_rate_segments_and_animation_interpolation(self):
        self.model=from_project(self.project);c=self.model['tracks'][0]['clips'][0];c['speed_keys']=[[0,.5],[1,2]]
        self.assertAlmostEqual(clip_duration(c),2.5);file,info=self.render();self.assertAlmostEqual(float(info['format']['duration']),2.5,delta=.12)
        self.assertEqual(value_at({'keys':{'x':[[0,0],[2,10]]}},'x',1),5)
    def test_audio_only_export(self):
        self.model=from_project(self.project);file,info=self.render('mix.wav',{'audio_only':True});self.assertFalse(any(s['codec_type']=='video' for s in info['streams']))
    def test_opacity_transition_and_blur(self):
        self.model=from_project(self.project);self.model['tracks'][1]['clips'][0].update(x=0,y=0,w=160,h=90,fade_in=1)
        self.model['annotations']=[{'id':'blur','kind':'blur','start':1.2,'end':2,'x':0,'y':0,'w':70,'h':70}]
        file,info=self.render();pixel=self.pixel(file,30,20,.5);self.assertGreater(pixel[0],50);self.assertGreater(pixel[2],50)

class StudioModelCases(unittest.TestCase):
    def test_segmented_speed_split_preserves_source_ranges_and_duration(self):
        from katman.studio_model import split_clip,clip_duration,trim_clip
        c={'id':'a','source_id':'media','name':'clip','start':0,'in':1,'length':4,'duration':4,'speed':1,'speed_keys':[[0,.5],[2,2]],'keys':{'x':[[0,0],[5,100]]}}
        model={'tracks':[{'clips':[c]}]};total=clip_duration(c);new=split_clip(model,'a',2)
        a,b=model['tracks'][0]['clips'];self.assertAlmostEqual(clip_duration(a)+clip_duration(b),total);self.assertAlmostEqual(b['in'],2);self.assertAlmostEqual(b['keys']['x'][0][1],40)
        trim_clip(b,'left',.5);self.assertGreater(b['in'],2)
    def test_reverse_split_preserves_playback_order(self):
        c={'id':'a','source_id':'media','start':0,'in':0,'length':4,'duration':4,'speed':1,'reverse':True}
        model={'tracks':[{'clips':[c]}]};split_clip(model,'a',1);a,b=model['tracks'][0]['clips'];self.assertEqual((a['in'],a['length']),(3,1));self.assertEqual((b['in'],b['length']),(0,3))

    def test_slicing_preserves_animation_boundary_and_fade_clock(self):
        from katman.studio_model import trim_clip
        c={'id':'a','start':0,'in':0,'length':4,'duration':4,'speed':1,'fade_in':2,'keys':{'x':[[0,0],[4,100]]}}
        m={'tracks':[{'clips':[c]}]};split_clip(m,'a',2);a,b=m['tracks'][0]['clips']
        self.assertEqual(value_at(a,'x',1),25);self.assertEqual(value_at(a,'x',2),50)
        self.assertEqual(value_at(b,'x',1),75);self.assertEqual(b['effect_offset'],2)
        trim_clip(b,'left',.5);self.assertEqual(value_at(b,'x',0),62.5);self.assertEqual(value_at(b,'x',1.5),100);self.assertEqual(b['effect_offset'],2.5)
    def test_frozen_clip_trim_changes_hold_duration(self):
        from katman.studio_model import trim_clip
        c={'id':'a','start':2,'in':0,'length':4,'duration':4,'speed':1,'freeze':True,'freeze_at':1}
        trim_clip(c,'right',-1);self.assertEqual(clip_duration(c),3);self.assertEqual(c['freeze_at'],1)
        trim_clip(c,'right',2);self.assertEqual(clip_duration(c),5)
