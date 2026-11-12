# SPDX-License-Identifier: GPL-3.0-only
"""Real generated-media assertions; timings belong to benchmark-preview.py."""
import io
import copy
import tempfile
from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_export
from katman import editor_worker as worker, studio_render
from katman.common import atomic_json
from katman.studio_model import from_project
from katman.studio_render import compile_graph, media_fingerprint, export_studio
from katman.preview_stream import stream
from katman.studio_jobs import preview_options


class PreviewStream(unittest.TestCase):
    setUp=test_export.ExportIntegration.setUp
    tearDown=test_export.ExportIntegration.tearDown

    def request(self, **values):
        model=from_project(self.project);self.project['studio']=model;atomic_json(self.path,self.project)
        return dict(path=str(self.path),model=model,position=0,size=[250,160],fps=24,audio=False,**values)

    def test_packed_frames_have_exact_size_privacy_and_no_container(self):
        payload=self.request()
        payload['model']['annotations']=[dict(id='privacy',kind='redact',start=0,end=2,x=10,y=10,w=40,h=30)]
        output=io.BytesIO();stream(payload,output);data=output.getvalue();frame_size=250*160*4
        self.assertEqual(len(data),48*frame_size)
        self.assertLess(max(data[(20*250+20)*4:(20*250+20)*4+3]),20)
        self.assertGreater(data[(60*250+60)*4],180)
        self.assertGreater(data[(30*250+180)*4+2],180)
        self.assertEqual(data[3],255)

    def test_requested_odd_dimensions_and_window(self):
        payload=self.request(window=.25);payload['size']=[101,65]
        output=io.BytesIO();stream(payload,output)
        self.assertEqual(len(output.getvalue()),6*101*65*4)

    def test_continuous_stream_has_no_thirty_second_cutoff(self):
        payload=self.request();payload.update(size=[8,8],fps=1)
        for track in payload['model']['tracks']:
            for clip in track['clips']:clip.update(freeze=True,freeze_at=0,duration=31)
        output=io.BytesIO();stream(payload,output)
        self.assertEqual(len(output.getvalue()),31*8*8*4)

    def test_reverse_frame_graph_decodes_one_frame_and_has_no_audio(self):
        payload=self.request();model=payload['model'];model['tracks'][0]['clips'][0]['reverse']=True
        args=compile_graph(self.root,self.project,model,self.root,{'size':[250,160]},(.5,.55),frame=True)
        graph=(self.root/'graph.ffscript').read_text()
        self.assertIn('trim=end_frame=1',graph);self.assertNotIn('reverse',graph);self.assertNotIn('anull',graph)
        self.assertNotIn('[aout]',args)
        self.assertEqual(sum(str(a).endswith('.mkv') for a in args),2)

    def test_reverse_paused_pixels_match_requested_source_time(self):
        payload=self.request();file=self.root/'media/changing.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=160x90:r=30:d=1','-f','lavfi','-i','color=blue:s=160x90:r=30:d=1','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264',str(file)],check=True)
        self.hashes[file.name]=hashlib.sha256(file.read_bytes()).digest()
        self.project['sources'][0]['file']='media/changing.mkv';atomic_json(self.path,self.project)
        payload['model']['tracks'][0]['clips'][0]['reverse']=True
        for i,position in enumerate((0,1.5)):
            destination=self.root/f'reverse{i}.png'
            export_studio(self.path,destination,{'size':[250,160]},model=payload['model'],window=(position,position+.05),frame=True)
            frame=subprocess.check_output(['ffmpeg','-v','error','-i',str(destination),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'])
            pixel=frame[(30*250+30)*3:(30*250+30)*3+3]
            self.assertGreater(pixel[2 if i==0 else 0],180)

    def test_scene_without_audio_does_not_launch_silent_sink(self):
        payload=self.request(window=.1);payload['audio']=True
        payload['model']['tracks']=[track for track in payload['model']['tracks'] if track['kind']=='video']
        player=self.root/'paplay';marker=self.root/'unneeded-audio'
        player.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
        player.chmod(0o755)
        with patch.dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ['PATH']):
            output=io.BytesIO();stream(payload,output)
        self.assertGreater(len(output.getvalue()),0);self.assertFalse(marker.exists())

    def test_audio_is_real_pcm_from_same_timeline(self):
        payload=self.request(window=.25);payload['audio']=True
        player=self.root/'paplay';capture=self.root/'pcm.raw'
        player.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nPath('+repr(str(capture))+').write_bytes(sys.stdin.buffer.read())\n')
        player.chmod(0o755)
        with patch.dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ['PATH']):
            output=io.BytesIO();stream(payload,output)
        pcm=capture.read_bytes();self.assertEqual(len(pcm),48000*4//4)
        self.assertTrue(any(pcm));self.assertEqual(len(output.getvalue()),6*250*160*4)

    def test_source_rate_defaults_deliver_real_sixty_and_one_twenty_frames(self):
        for fps in (60,120):
            with self.subTest(fps=fps):
                payload=self.request();file=self.root/f'media/rate{fps}.mkv'
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'nullsrc=s=64x36:r={fps}:d=0.2,geq=lum=20+N*8:cb=128:cr=128','-c:v','ffv1',str(file)],check=True)
                self.hashes[file.name]=hashlib.sha256(file.read_bytes()).digest()
                self.project['sources'][0].update(file=f'media/rate{fps}.mkv',width=64,height=36,media_duration=.2)
                atomic_json(self.path,self.project)
                model=payload['model'];model.update(width=64,height=36,fps=fps);model['tracks']=model['tracks'][:1]
                model['tracks'][0]['clips'][0].update(length=.2,duration=.2,w=64,h=36)
                payload.pop('fps');payload.pop('size')
                output=io.BytesIO();stream(payload,output);raw=output.getvalue();stride=64*36*4
                self.assertEqual(len(raw),round(fps*.2)*stride)
                values=[raw[i] for i in range(0,len(raw),stride)]
                self.assertEqual(len(set(values)),len(values))
                self.assertEqual(values,sorted(values))

    def test_preview_quality_is_explicit_and_source_rate_is_capped(self):
        model={'width':3840,'height':2160,'fps':120}
        native=preview_options(model);self.assertEqual(native['size'],(3840,2160));self.assertEqual(native['fps'],120);self.assertFalse(native['proxy'])
        selected=preview_options(model,size=[1920,1080],fps=60,proxy=True)
        self.assertEqual(selected['size'],(1920,1080));self.assertEqual(selected['fps'],60);self.assertTrue(selected['proxy'])
        self.assertEqual(preview_options({**model,'fps':240})['fps'],120)
        for size in ([0,1080],[8192,8192],[1920]):
            with self.assertRaises(ValueError):preview_options(model,size=size)

    def test_cancel_owned_group_reaps_decoder(self):
        payload=self.request();payload['fps']=120
        for track in payload['model']['tracks']:
            if track['kind']=='audio':track['mute']=True
            for clip in track['clips']:clip.update(freeze=True,freeze_at=0,duration=120)
        process=subprocess.Popen([sys.executable,'-m','katman.preview_stream'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            process.stdin.write(json.dumps(payload).encode());process.stdin.close()
            self.assertEqual(len(process.stdout.read(250*160*4)),250*160*4)
            os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=8)
            # The worker owns and waits for FFmpeg even when killed while the
            # native reader leaves a full frame pipe pending.
            with self.assertRaises(ProcessLookupError):os.killpg(process.pid,0)
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            process.stdout.close();process.stderr.close()

    def test_cache_fingerprint_changes_when_source_replaced(self):
        payload=self.request();before=media_fingerprint(self.path,self.project,payload['model'])
        source=self.root/self.project['sources'][0]['file'];stat=source.stat();os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000))
        self.assertNotEqual(before,media_fingerprint(self.path,self.project,payload['model']))


class SnapshotPreview(unittest.TestCase):
    tearDown=test_export.ExportIntegration.tearDown
    request=PreviewStream.request

    def setUp(self):
        test_export.ExportIntegration.setUp(self)
        cache=self.root/'cache';cache.mkdir()
        for module in (worker,studio_render):
            patcher=patch.object(module,'CACHE',cache);patcher.start();self.addCleanup(patcher.stop)

    def frame(self,payload):
        return worker.request({**payload,'operation':'frame','width':250})

    def pixel(self,file):
        raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(file),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'])
        return raw[(30*250+30)*3:(30*250+30)*3+3]

    def test_snapshot_frame_renders_without_a_saved_project_file(self):
        payload=self.request();payload['project_snapshot']=copy.deepcopy(self.project);self.path.unlink()
        result=self.frame(payload);self.assertTrue(result['ok'],result)
        self.assertGreater(self.pixel(result['data']['file'])[0],180)
        self.assertFalse(self.path.exists())

    def test_snapshot_sources_override_stale_disk_and_invalidate_frame_cache(self):
        payload=self.request();payload['project_snapshot']=copy.deepcopy(self.project)
        first=self.frame(payload);self.assertTrue(first['ok'],first)
        payload['project_snapshot']['sources'][0]['file']='media/right.mkv'
        second=self.frame(payload);self.assertTrue(second['ok'],second)
        self.assertNotEqual(first['data']['file'],second['data']['file'])
        self.assertGreater(self.pixel(first['data']['file'])[0],180)
        self.assertGreater(self.pixel(second['data']['file'])[2],180)
        # A corrupt disk document must neither affect rendering nor be rewritten.
        self.path.write_text('stale invalid disk JSON')
        third=self.frame(payload);self.assertTrue(third['ok'],third)
        self.assertEqual(third['data']['file'],second['data']['file'])
        self.assertEqual(self.path.read_text(),'stale invalid disk JSON')

    def test_invalid_snapshot_shapes_are_rejected(self):
        payload=self.request()
        snapshots=[[],{},dict(format='katman',version=1,sources={})]
        for source in (None,{},dict(id='bad',type='video',file=3),dict(id='bad',type='video',file=''),dict(id='bad',type='video',file='bad\0file')):
            snapshots.append(dict(format='katman',version=1,sources=[source]))
        duplicate=copy.deepcopy(self.project);duplicate['sources'].append(copy.deepcopy(duplicate['sources'][0]));snapshots.append(duplicate)
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                result=self.frame({**payload,'project_snapshot':snapshot})
                self.assertFalse(result['ok']);self.assertIn('snapshot is invalid',result['error'])

    def test_snapshot_rejects_parent_absolute_and_symlink_escapes(self):
        payload=self.request()
        with tempfile.TemporaryDirectory() as outside:
            external=Path(outside)/'source.mkv';external.write_bytes(b'outside')
            link=self.root/'escape.mkv';link.symlink_to(external)
            for file in ('../outside.mkv',str(external),'escape.mkv'):
                snapshot=copy.deepcopy(self.project);snapshot['sources'][0]['file']=file
                result=self.frame({**payload,'project_snapshot':snapshot})
                self.assertFalse(result['ok']);self.assertIn('inside the project folder',result['error'])

    def test_frame_quality_size_changes_cache_and_exact_output(self):
        payload=self.request();payload['project_snapshot']=copy.deepcopy(self.project)
        first=self.frame({**payload,'size':[250,160],'fps':60});second=self.frame({**payload,'size':[500,320],'fps':120})
        self.assertTrue(first['ok'],first);self.assertTrue(second['ok'],second)
        self.assertNotEqual(first['data']['file'],second['data']['file'])
        for result,size in ((first,[250,160]),(second,[500,320])):
            data=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=width,height','-of','json',result['data']['file']]))['streams'][0]
            self.assertEqual([data['width'],data['height']],size)

    def test_snapshot_raw_stream_does_not_load_disk_project(self):
        payload=self.request(window=.125);payload['project_snapshot']=copy.deepcopy(self.project)
        payload['project_snapshot']['sources'][0]['file']='media/right.mkv';self.path.unlink()
        output=io.BytesIO();stream(payload,output);raw=output.getvalue()
        self.assertEqual(len(raw),3*250*160*4)
        self.assertGreater(raw[(30*250+30)*4+2],180);self.assertFalse(self.path.exists())
