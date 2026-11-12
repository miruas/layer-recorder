# SPDX-License-Identifier: GPL-3.0-only
"""Compare real generated media against the unchanged compositor graph."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from PIL import Image
from katman.common import atomic_json
from katman.studio_model import from_project
from katman.studio_render import compile_graph,export_studio,video_encoder_options


class ExportOptimization(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'media').mkdir()
        sources=[]
        for i in range(2):
            file=f'media/source{i}.mkv'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=128x96:r=24:d=0.5','-vf',('hflip' if i else 'null'),'-c:v','ffv1','-threads','1',str(self.root/file)],check=True)
            sources.append(dict(id=str(i),name=str(i),file=file,type='video',width=128,height=96,x=i*128,y=0,logical_width=128,logical_height=96,media_duration=.5,timeline_start=0,trim_in=0))
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.5:sample_rate=48000','-c:a','pcm_s16le',str(self.root/'media/audio.wav')],check=True)
        sources.append(dict(id='audio',name='Audio',file='media/audio.wav',type='audio',media_duration=.5,timeline_start=0,trim_in=0))
        self.project=dict(format='katman',version=1,duration=.5,fps=24,sources=sources,sections=[]);self.model=from_project(self.project);self.path=self.root/'project.katman';atomic_json(self.path,self.project)

    def graph(self,model=None,options=None,optimize=True):
        folder=self.root/('optimized' if optimize else 'reference');folder.mkdir(exist_ok=True)
        options={'video_only':not (options or {}).get('audio_only'),'pixel_format':'rgba',**(options or {}),'optimize_graph':optimize}
        args=compile_graph(self.root,self.project,model or self.model,folder,options)
        return args,(folder/'graph.ffscript').read_text()

    def raw(self,model=None,options=None,optimize=True):
        args,_=self.graph(model,options,optimize)
        args+=['-threads','1']
        if options and options.get('audio_only'):args+=['-c:a','pcm_s16le','-ar','48000','-f','s16le','pipe:1']
        else:args+=['-c:v','rawvideo','-pix_fmt','rgba','-f','rawvideo','pipe:1']
        return subprocess.check_output(args)

    def parity(self,model=None,options=None):
        expected=self.raw(model,options,False);actual=self.raw(model,options,True)
        self.assertEqual(actual,expected);self.assertTrue(actual)

    def test_static_resize_and_identity_final_filters_are_simplified_without_pixel_changes(self):
        _,graph=self.graph();self.assertIn('scale=128:96:eval=init:flags=bilinear',graph)
        self.assertNotIn('force_original_aspect_ratio',graph);self.assertNotIn('pad=',graph)
        # This dimension-identity scale still converts RGB -> subsampled YUV
        # with bilinear sampling; dropping it would change source colors.
        self.assertIn('format=rgba,scale=128:96',graph)
        self.parity();self.parity(options={'size':[192,72]});self.parity(options={'size':[257,101]})
        constant=copy.deepcopy(self.model);constant['tracks'][0]['clips'][0]['keys']={'w':[[0,128],[.5,128]],'h':[[0,96],[.5,96]]}
        self.assertIn('scale=128:96:eval=init',self.graph(constant)[1]);self.parity(constant)

    def test_rgb_opacity_identity_scale_and_vector_identity_resize_are_safe_to_remove(self):
        image=Image.new('RGB',(128,96),(255,80,10));image.paste((20,80,240),(12,14,100,72));image.save(self.root/'media/still.png')
        self.project['sources'][0]['file']='media/still.png'
        model=copy.deepcopy(self.model);model['tracks'][0]['clips'][0]['opacity']=.5
        model['annotations']=[dict(kind='rectangle',start=0,end=.5,x=20,y=10,w=70,h=40,color='#80ccff',width=.05,points=[[.1,.1],[.8,.8]])]
        _,graph=self.graph(model);_,reference=self.graph(model,optimize=False)
        self.assertLess(graph.count('scale='),reference.count('scale='));self.assertIn('format=rgba,setsar=1,colorchannelmixer',graph)
        self.assertIn('[2:v]format=rgba[annotation0image]',graph)
        self.parity(model)

    def test_dynamic_geometry_crop_rotation_privacy_and_captions_match_every_frame(self):
        model=copy.deepcopy(self.model)
        model['tracks'][0]['clips'][0].update(crop_x=.1,crop_w=.8,rotation=12,keys={'w':[[0,120],[.5,150]],'h':[[0,96],[.5,80]]})
        model['tracks'][1]['clips'][0].update(w=110,h=80,x=130,brightness=.12,fade_in=.2)
        model['annotations']=[dict(kind='blur',start=0,end=.5,x=20,y=15,w=70,h=55),dict(kind='pixelate',start=.1,end=.4,x=100,y=10,w=55,h=50),dict(kind='text',text='100% {literal}',font_size=14,start=0,end=.5,x=5,y=3)]
        model['annotations'].append(dict(kind='arrow',start=0,end=.5,x=160,y=30,w=60,h=50,color='#40ff80',width=.04,points=[[.1,.1],[.9,.8]],keys={'w':[[0,60],[.5,80]],'opacity':[[0,.3],[.5,.8]]}))
        model['subtitles']=[dict(text='Caption Şğ',start=0,end=.5)];model['subtitle_style'].update(size=16,margin_bottom=4)
        _,graph=self.graph(model);self.assertIn('eval=frame:flags=bilinear',graph);self.assertIn('maskedmerge',graph);self.assertIn('ass=filename',graph)
        self.parity(model)

    def test_alpha_sources_and_actual_geometry_are_preserved(self):
        image=Image.new('RGBA',(128,96),(255,70,20,100));image.paste((20,90,255,255),(20,20,90,80));image.save(self.root/'media/alpha.png')
        self.project['sources'][0]['file']='media/alpha.png'
        model=copy.deepcopy(self.model);model['tracks'][0]['clips'][0]['opacity']=.6
        self.parity(model)
        self.project['sources'][0]['width']=256
        model['tracks'][0]['clips'][0]['w']=256
        _,graph=self.graph(model);self.assertIn('scale=256:96:eval=init',graph)
        self.parity(model)

    def test_audio_mix_fades_and_denoising_are_unchanged(self):
        model=copy.deepcopy(self.model);model['tracks'][2]['clips'][0].update(gain=.6,fade_in=.1,fade_out=.1,denoise=True)
        self.parity(model,{'audio_only':True})

    def test_codec_specific_speed_arguments_keep_requested_quality(self):
        for preset in ('p1','p2','p3','p4','p5','p6','p7'):
            args=video_encoder_options({'codec':'h264_nvenc','preset':preset,'quality':19})
            self.assertEqual(args[args.index('-preset')+1],preset);self.assertEqual(args[args.index('-cq')+1],'19');self.assertNotIn('-crf',args)
        args=video_encoder_options({'codec':'hevc_vaapi','quality':19,'vaapi_quality':5})
        self.assertEqual(args[args.index('-qp')+1],'19');self.assertEqual(args[args.index('-quality')+1],'5');self.assertNotIn('-preset',args)
        default=video_encoder_options({});self.assertEqual(default,['-c:v','libx264','-preset','fast','-crf','20'])
        self.assertEqual(video_encoder_options({'codec':'libaom-av1','cpu_used':8,'quality':31})[-4:],['-crf','31','-b:v','0'])
        for options in ({'codec':'h264_nvenc','preset':'ultrafast'},{'codec':'h264_vaapi','preset':'fast'},{'codec':'h264_vaapi','vaapi_quality':True},{'codec':'libx264','preset':'p1'},{'codec':'libaom-av1','cpu_used':9}):
            with self.assertRaises(ValueError):video_encoder_options(options)

    def test_thread_options_are_explicit_and_validated(self):
        args,_=self.graph(options={'filter_threads':0});self.assertEqual(args[args.index('-filter_complex_threads')+1],'0')
        for invalid in (-1,65,True,'8'):
            with self.assertRaises(ValueError):self.graph(options={'filter_threads':invalid})
        self.assertIn('log-level=error',video_encoder_options({'codec':'libx265','threads':0}))
        self.assertNotIn('pools=4',','.join(video_encoder_options({'codec':'libx265','threads':0})))

    def test_real_x264_presets_preserve_lossless_frames_and_dimensions(self):
        outputs=[]
        for preset in ('ultrafast','fast'):
            file=self.root/(preset+'.mp4')
            export_studio(self.path,file,{'codec':'libx264','preset':preset,'quality':0,'threads':2},model=self.model)
            outputs.append(subprocess.check_output(['ffmpeg','-v','error','-i',str(file),'-an','-pix_fmt','yuv420p','-f','rawvideo','-']))
        self.assertEqual(outputs[0],outputs[1]);self.assertEqual(len(outputs[0]),12*256*96*3//2)
