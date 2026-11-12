# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from katman import overlay_raster
from katman.studio_render import font_file, quote


class OverlayRaster(unittest.TestCase):
    def request(self, **changes):
        return dict(font='Noto Sans', text='Hello\nSecond line', size_px=24, width=257, height=100, **changes)

    def test_rgba_has_real_transparency_white_glyph_and_black_outline(self):
        data = overlay_raster.rasterize(self.request())
        self.assertEqual(len(data), 257*100*4)
        pixels = list(zip(data[::4], data[1::4], data[2::4], data[3::4]))
        self.assertEqual(pixels[-1][3], 0)
        self.assertTrue(any(r == g == b == 255 and a == 255 for r,g,b,a in pixels))
        self.assertTrue(any(r == g == b == 0 and a == 255 for r,g,b,a in pixels))
        self.assertTrue(any(0 < a < 255 for r,g,b,a in pixels))

    def test_sprite_matches_drawtext_export_on_opaque_background(self):
        request = self.request()
        sprite = overlay_raster.rasterize(request)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'text.txt';path.write_text(request['text'])
            draw=(f"drawtext=fontfile='{quote(font_file(request['font']))}':textfile='{quote(path)}':"
                  f"fontsize={request['size_px']}:fontcolor=white:borderw=1:bordercolor=black:x=2:y=2")
            golden=subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=257x100:r=1,format=rgba','-vf',draw,'-frames:v','1','-threads','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'])
        # Straight-alpha sprite composited onto black matches full drawtext.
        actual=bytearray(len(sprite))
        for offset in range(0,len(sprite),4):
            alpha=sprite[offset+3]
            for channel in range(3):actual[offset+channel]=round(sprite[offset+channel]*alpha/255)
            actual[offset+3]=255
        differences=[abs(a-b) for a,b in zip(actual,golden)]
        self.assertLessEqual(max(differences),2)

    def test_literal_text_and_cli_exact_framing(self):
        request=self.request();request['text']="100% %{n} \\ ' : Turkish Şğ"
        run=subprocess.run([sys.executable,'-m','katman.overlay_raster'],input=json.dumps(request).encode(),capture_output=True)
        self.assertEqual(run.returncode,0,run.stderr.decode())
        self.assertEqual(len(run.stdout),257*100*4)
        self.assertTrue(any(run.stdout[3::4]))

    def test_invalid_requests_are_rejected_before_font_or_ffmpeg_work(self):
        for changes in [dict(width=4097),dict(height=2049),dict(width=True),dict(size_px=1001),dict(text='x'*65537),dict(text='x\0y'),dict(font='bad\nfont')]:
            request=self.request();request.update(changes)
            with self.assertRaises(ValueError):overlay_raster.rasterize(request)


if __name__ == '__main__':unittest.main()
