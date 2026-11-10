import copy
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
import test_export
from katman.common import atomic_json,probe
from katman.export import export_project,sections_for
from katman.editing import import_recording,join_next
from katman.identity import application_label


class EditingIntegration(unittest.TestCase):
    setUp=test_export.ExportIntegration.setUp
    def tearDown(self):
        for name,digest in self.hashes.items():
            self.assertEqual(hashlib.sha256((self.root/'media'/name).read_bytes()).digest(),digest)
        self.temp.cleanup()

    def render(self,section,name):
        self.project['sections']=[section];atomic_json(self.path,self.project)
        dest=self.root/name;export_project(self.path,dest);info=probe(dest)
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        raw=subprocess.check_output(['ffmpeg','-v','error','-ss','0.25','-i',str(dest),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
        width=video['width']
        return info,lambda x,y:tuple(raw[(y*width+x)*3:(y*width+x)*3+3])

    def test_display_blackout_and_removal_preserve_expected_pixels(self):
        sec=sections_for(self.project)[0];sec['blackout_ids']=['right']
        info,pixel=self.render(sec,'blackout.mp4')
        self.assertGreater(pixel(20,20)[0],200);self.assertLess(max(pixel(200,20)),15)
        sec['enabled_ids']=['left'];info,pixel=self.render(sec,'removed.mp4')
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(160,90))

    def test_placement_stacking_crop_drawing_and_privacy_export(self):
        sec=sections_for(self.project)[0];sec['placements']={'right':[40,0,90,90]}
        sec['crop']=[.25,0,.5,1]
        sec['annotations']=[{'tool':'redact','color':'#000000','width':.004,'points':[[.2,.2],[.4,.4]]},
                            {'tool':'pen','color':'#00ff00','width':.05,'points':[[.1,.7],[.9,.7]]}]
        info,pixel=self.render(sec,'effects.mp4');video=next(s for s in info['streams'] if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(80,90))
        self.assertGreater(pixel(5,10)[2],180)
        self.assertLess(max(pixel(24,27)),20)
        self.assertGreater(pixel(40,63)[1],150)
        sec['crop']=[0,0,1,1];sec['annotations']=[];sec['layer_order']=['right','left']
        _,pixel=self.render(sec,'stacked.mp4');self.assertGreater(pixel(50,20)[0],180)

    def test_speed_keeps_clip_duration_in_sync(self):
        sec=sections_for(self.project)[0];sec['speed']=2
        info,_=self.render(sec,'fast.mp4');self.assertAlmostEqual(float(info['format']['duration']),1,delta=.1)

    def test_append_and_overlay_import_preserve_original_media(self):
        self.project['sections']=sections_for(self.project);atomic_json(self.path,self.project)
        other=self.root/'other';other.mkdir();(other/'media').mkdir()
        import shutil
        for file in (self.root/'media').iterdir():shutil.copyfile(file,other/'media'/file.name)
        other_meta=copy.deepcopy(self.project)
        for index,name in enumerate(('left','right')):
            sub=other/name;sub.mkdir();shutil.copyfile(other/'media'/(name+'.mkv'),sub/'shared.mkv')
            other_meta['sources'][index]['file']=name+'/shared.mkv'
        other_path=other/'project.katman';atomic_json(other_path,other_meta)
        joined=import_recording(self.path,self.project,other_path)
        self.assertEqual(joined['duration'],4);self.assertEqual(len(joined['sections']),2)
        self.assertNotEqual(joined['sources'][4]['file'],joined['sources'][5]['file'])
        for index,name in enumerate(('left','right')):
            self.assertEqual(hashlib.sha256((self.root/joined['sources'][4+index]['file']).read_bytes()).digest(),self.hashes[name+'.mkv'])
        atomic_json(self.path,joined);dest=self.root/'joined.mp4';export_project(self.path,dest)
        self.assertAlmostEqual(float(probe(dest)['format']['duration']),4,delta=.15)
        overlay=import_recording(self.path,self.project,other_path,True)
        self.assertEqual(sum(s['type']=='video' for s in overlay['sources']),3)
        self.assertTrue(overlay['sections'][0]['placements'])
        atomic_json(self.path,overlay);export_project(self.path,self.root/'overlay.mp4')

    def test_join_refuses_to_restore_a_removed_interval(self):
        sec=sections_for(self.project)[0];sec['end']=1
        second=copy.deepcopy(sec);second.update(start=1,end=2)
        self.project['sections']=[sec,second];join_next(self.project,0)
        self.assertEqual(self.project['sections'][0]['end'],2)
        self.project['sections']=[dict(sec,end=.8),dict(second,start=1)]
        with self.assertRaises(ValueError):join_next(self.project,0)

    def test_titles_are_never_used_for_application_names(self):
        for app,expected in [('org.mozilla.firefox','Firefox'),('discord','Discord'),('org.vinegarhq.Sober','Sober')]:
            self.assertEqual(application_label({'class':app,'title':'Private secret tab'}),expected)
        self.assertIsNone(application_label({'class':'katman','title':'Firefox'}))
        self.assertNotIn('/',application_label({'class':'../../Example'}))
