# SPDX-License-Identifier: GPL-3.0-only
"""A correct MP4 first frame does not establish an exact stream-copy trim.

This generated GOP/B-frame fixture retains input-seek decoder preroll through
MP4 edit lists, but `-t` with stream copy includes presentation frames beyond
the requested end. Use an accurate render for partial video cuts; do not infer
whole-cut equivalence from the first-frame equality demonstrated here.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest


class StreamCopyTiming(unittest.TestCase):
    def test_non_keyframe_mp4_first_frame_matches_but_tail_is_not_an_exact_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.mp4';copy=root/'trimmed.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','nullsrc=s=96x64:r=30:d=2','-vf','geq=lum=20+3*N:cb=100:cr=150','-c:v','libx264','-preset','fast','-g','30','-keyint_min','30','-sc_threshold','0','-bf','3','-threads','1',str(source)],check=True)
            subprocess.run(['ffmpeg','-v','error','-ss','0.3','-i',str(source),'-t','0.7','-map','0:v:0','-c:v','copy',str(copy)],check=True)
            def frame(file,seek=None):
                args=['ffmpeg','-v','error']
                if seek is not None:args+=['-ss',str(seek)]
                return subprocess.check_output(args+['-i',str(file),'-frames:v','1','-pix_fmt','rgb24','-threads','1','-f','rawvideo','pipe:1'])
            accurate=frame(source,.3);actual=frame(copy)
            self.assertEqual(actual,accurate)
            self.assertNotEqual(actual,frame(source,0))
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=start_time:packet=pts_time,flags','-of','json',str(copy)]))
            self.assertAlmostEqual(float(info['streams'][0]['start_time']),0,places=6)
            # Negative preroll packets are intentional; do not apply a blanket
            # make_zero timestamp shift, which changes the visible start.
            self.assertTrue(any(float(packet.get('pts_time',0))<0 for packet in info['packets']))
            self.assertTrue(any('D' in packet.get('flags','') for packet in info['packets']))
            def decoded(file,seek=None,span=None):
                args=['ffmpeg','-v','error']
                if seek is not None:args+=['-ss',str(seek)]
                args+=['-i',str(file)]
                if span is not None:args+=['-t',str(span)]
                return subprocess.check_output(args+['-an','-pix_fmt','rgb24','-threads','1','-f','rawvideo','pipe:1'])
            wanted=decoded(source,.3,.7);copied=decoded(copy);stride=96*64*3
            self.assertEqual(len(wanted)//stride,21)
            self.assertGreater(len(copied)//stride,21,'The GOP/B-frame fixture should demonstrate trailing-frame stream-copy imprecision.')
            self.assertNotEqual(copied[-stride:],wanted[-stride:])
            self.assertTrue(any(float(packet.get('pts_time',0))>=.7 for packet in info['packets']))


if __name__=='__main__':unittest.main()
