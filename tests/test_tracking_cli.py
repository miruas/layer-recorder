# SPDX-License-Identifier: GPL-3.0-only
"""Tracking uses the shipped decoder and keeps cancellation/source ownership."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from katman.studio_jobs import _tracking_frames, track_region


class TrackingCli(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.video=self.root/'moving.mkv'
        rng=np.random.default_rng(42)
        texture=rng.integers(30,255,size=(32,32,3),dtype=np.uint8)
        frames=[]
        for index in range(20):
            frame=np.zeros((96,160,3),dtype=np.uint8)
            frame[20:52,20+index*2:52+index*2]=texture
            frames.append(frame.tobytes())
        subprocess.run(['ffmpeg','-v','error','-f','rawvideo','-pixel_format','bgr24',
                        '-video_size','160x96','-framerate','10','-i','pipe:0',
                        '-an','-c:v','ffv1',str(self.video)],input=b''.join(frames),check=True)

    def test_tracks_textured_motion_without_opencv_videoio(self):
        import cv2
        annotation={'id':'privacy','start':0,'end':2,'x':20,'y':20,'w':32,'h':32}
        model={'width':160,'height':96,'annotations':[annotation],'subtitles':[]}
        original=self.video.read_bytes();progress=[]
        def fixture(_path,destination,*_args,**_kwargs):shutil.copy2(self.video,destination)
        # Any dependency on a wheel's hidden video decoder would fail this test.
        with patch('katman.studio_jobs.export_studio',side_effect=fixture), patch.object(cv2,'VideoCapture',side_effect=AssertionError('VideoIO must not be used'),create=True):
            keys=track_region(self.root/'project.katman',model,annotation,progress=progress.append)
        self.assertGreater(len(keys['x']),10)
        self.assertAlmostEqual(keys['x'][-1][1],58,delta=1.5)
        self.assertAlmostEqual(keys['y'][-1][1],20,delta=1.5)
        self.assertTrue(all(0<=value<=1 for value in progress))
        self.assertEqual(self.video.read_bytes(),original)
        self.assertEqual(annotation['x'],20)

    def test_decoder_cancel_closes_process_and_keeps_original(self):
        cancelled=threading.Event();cancelled.set()
        with self.assertRaisesRegex(RuntimeError,'Tracking cancelled'):
            with _tracking_frames(self.video,cancelled) as (_w,_h,frames):next(frames)
        self.assertTrue(self.video.is_file())
