import csv
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from katman.buffer_storage import create_ram_buffer,filesystem_type
from katman.capture import Track,Recorder
from katman.common import run,probe


class RamReplay(unittest.TestCase):
    def test_private_memory_directory_and_no_disk_fallback(self):
        root=create_ram_buffer()
        try:
            self.assertEqual(filesystem_type(root),'tmpfs')
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode),0o700)
            self.assertEqual(root.stat().st_uid,os.getuid())
        finally:shutil.rmtree(root)
        with patch('katman.buffer_storage.filesystem_type',return_value='ext4'):
            with self.assertRaisesRegex(RuntimeError,'not a memory filesystem'):create_ram_buffer()
        with patch('katman.buffer_storage.shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)):
            with self.assertRaisesRegex(RuntimeError,'512 MB'):create_ram_buffer()

    def track(self,root,ranges):
        t=Track.__new__(Track);t.id='qa';t.path=root/'qa';t.path.mkdir();t.offset=0;t.csv_offset=0;t.csv_pending='';t.rows=[]
        with (t.path/'segments.csv').open('w') as f:
            writer=csv.writer(f)
            for i,(start,end) in enumerate(ranges):writer.writerow([str(t.path/f'{i:08}.mkv'),start,end])
        return t

    def test_long_recording_spills_closed_segments_but_replay_ring_stays_in_ram(self):
        root=create_ram_buffer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                archive=Path(directory)/'recording';t=self.track(root,[(0,2),(302,304),(308,310)])
                for i in range(3):(t.path/f'{i:08}.mkv').write_bytes(bytes([i])*100)
                t.prune(-3,306,archive)
                self.assertEqual([f.parent for f,s,e in t.rows],[archive/'qa',archive/'qa',t.path])
                self.assertEqual([f.read_bytes() for f,s,e in t.rows],[bytes([i])*100 for i in range(3)])
                self.assertEqual(len(list(t.path.glob('*.mkv'))),1)
                t.prune(306)
                self.assertEqual([(s,e) for f,s,e in t.rows],[(308,310)])
                self.assertEqual(list(archive.rglob('*.mkv')),[])
        finally:shutil.rmtree(root)

    def test_save_concatenates_ram_and_ordinary_recording_spool(self):
        root=create_ram_buffer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                disk=Path(directory);t=self.track(root,[(0,2),(2,4)])
                t.meta={'type':'video','name':'QA display','width':64,'height':64};t.alive=lambda:False
                for i,color in enumerate(('red','blue')):
                    run(['ffmpeg','-v','error','-f','lavfi','-i',f'color={color}:s=64x64:r=10:d=2','-c:v','libx264','-preset','ultrafast',t.path/f'{i:08}.mkv'])
                t.prune(-1,2.5,disk/'spool');self.assertNotEqual(t.rows[0][0].parent,t.rows[1][0].parent)
                r=Recorder.__new__(Recorder);r.running=False;r.tracks=[t];r.errors=[]
                with patch('katman.capture.settings',return_value={'library':str(disk/'library')}),patch('katman.identity.focused_application',return_value='RAM QA'):
                    path=Path(r._snapshot(0,4,'recording'))
                project=json.loads(path.read_text());self.assertEqual(project['warnings'],[])
                media=path.parent/project['sources'][0]['file'];self.assertAlmostEqual(float(probe(media)['format']['duration']),4,delta=.1)
                import subprocess
                frame=subprocess.check_output(['ffmpeg','-v','error','-ss','3','-i',str(media),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
                self.assertGreater(frame[2],200);self.assertLess(frame[0],30)
                self.assertTrue(all(f.exists() for f,s,e in t.rows))
        finally:shutil.rmtree(root)
