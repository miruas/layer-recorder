# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in real AMD/Intel media checks; driver support is not emulated."""
import os,unittest
import test_studio

@unittest.skipUnless(os.environ.get('LAYER_TEST_VAAPI_DEVICE'),'Set LAYER_TEST_VAAPI_DEVICE for real VA-API encoding.')
class VaapiExports(unittest.TestCase):
    setUp=test_studio.StudioRender.setUp
    tearDown=test_studio.StudioRender.tearDown
    render=test_studio.StudioRender.render
    pixel=test_studio.StudioRender.pixel
    def test_h264_vaapi(self):
        from katman.studio_model import from_project
        self.model=from_project(self.project)
        file,info=self.render('vaapi-h264.mp4',{'codec':'h264_vaapi','vaapi_device':os.environ['LAYER_TEST_VAAPI_DEVICE'],'quality':24})
        self.assertEqual(info['streams'][0]['codec_name'],'h264');self.assertGreater(self.pixel(file,20,20)[0],180)
    def test_hevc_vaapi(self):
        from katman.studio_model import from_project
        self.model=from_project(self.project)
        file,info=self.render('vaapi-hevc.mkv',{'codec':'hevc_vaapi','vaapi_device':os.environ['LAYER_TEST_VAAPI_DEVICE'],'quality':24})
        self.assertEqual(info['streams'][0]['codec_name'],'hevc')
