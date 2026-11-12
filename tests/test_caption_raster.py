# SPDX-License-Identifier: GPL-3.0-only
"""Isolated generated-caption comparisons; no recorded media or UI required."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image,ImageChops
from katman import caption_raster
from katman.studio_render import caption_ass,quote,export_studio
from katman.common import atomic_json


def rgba_pixels(image):
    data=image.tobytes()
    return zip(data[::4],data[1::4],data[2::4],data[3::4])


class CaptionRaster(unittest.TestCase):
    def request(self,**changes):
        return dict(canvas=[400,240],width=400,height=240,captions=[{'text':'First caption'}],style={'font':'Noto Sans','size':30,'color':'#ff8000'},**changes)

    def full_sprite(self,request):
        header,pixels=caption_raster.rasterize(request)
        self.assertEqual(len(pixels),header['width']*header['height']*4)
        canvas=Image.new('RGBA',(request['width'],request['height']))
        canvas.paste(Image.frombytes('RGBA',(header['width'],header['height']),pixels),(header['x'],header['y']))
        return header,canvas

    def golden(self,request,background):
        captions=[{**row,'start':0,'end':1} for row in request['captions']]
        with tempfile.TemporaryDirectory() as directory:
            ass=Path(directory)/'captions.ass';ass.write_text(caption_ass(captions,request['style'],request['canvas']),encoding='utf8')
            data=subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',f'color={background}:s={request["width"]}x{request["height"]}:r=1,format=yuv444p','-vf',f"ass=filename='{quote(ass)}'",'-frames:v','1','-threads','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'])
            base=subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',f'color={background}:s={request["width"]}x{request["height"]}:r=1,format=yuv444p','-frames:v','1','-threads','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'])
        size=(request['width'],request['height'])
        return Image.frombytes('RGBA',size,data),Image.frombytes('RGBA',size,base)

    def test_transparency_full_colors_and_exact_cropped_reconstruction(self):
        request=self.request();header,sprite=self.full_sprite(request)
        alpha=sprite.getchannel('A');self.assertEqual(alpha.getbbox(),(header['x'],header['y'],header['x']+header['width'],header['y']+header['height']))
        self.assertLess(header['width']*header['height'],request['width']*request['height']//2)
        pixels=list(rgba_pixels(sprite))
        self.assertIn((255,128,0,255),pixels)
        self.assertTrue(any(0<a<255 and r==g==b==0 for r,g,b,a in pixels))
        self.assertEqual(sprite.getpixel((0,0)),(0,0,0,0))
        _,_,_,rows,style=caption_raster.validate(request)
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'a.ass';file.write_text(caption_ass(rows,style,request['canvas']))
            direct=subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i','color=black@0:s=400x240:r=1,format=yuva444p','-vf',f"ass=filename='{quote(file)}':alpha=1",'-frames:v','1','-threads','1','-pix_fmt','rgba','-f','rawvideo','-'])
        # Hidden RGB in fully transparent pixels is normalized by the crop.
        expected=Image.frombytes('RGBA',sprite.size,direct)
        self.assertIsNone(ImageChops.difference(sprite.getchannel('A'),expected.getchannel('A')).getbbox())
        for actual,wanted in zip(rgba_pixels(sprite),rgba_pixels(expected)):
            if wanted[3]:self.assertEqual(actual,wanted)

    def test_wrapping_margins_styles_overlap_utf8_and_literal_text_match_ass(self):
        cases=[]
        long=self.request();long['style'].update(size=26,margin_left=70,margin_right=45,margin_bottom=12)
        long['captions']=[{'text':'A long caption wraps within these margins and stays centered near the bottom.'}];cases.append(long)
        overlap=self.request();overlap['captions']=[{'text':'First row Türkçe Şğ İı 😀','style':{'color':'#40ff80','size':24}},{'text':'Second row\n100% {literal} \\N \\ %{n}', 'style':{'color':'#80a0ff','size':28,'margin_bottom':50}}];cases.append(overlap)
        scaled=self.request();scaled.update(canvas=[800,480],width=400,height=240);scaled['style'].update(size=56,margin_left=80,margin_right=60,margin_bottom=44);scaled['captions']=[{'text':'Scaled canvas\nwith margins'}];cases.append(scaled)
        for request in cases:
            _,sprite=self.full_sprite(request)
            for background in ('black','0x24588c'):
                with self.subTest(captions=request['captions'],background=background):
                    golden,base=self.golden(request,background)
                    actual=Image.alpha_composite(base,sprite)
                    differences=list(rgba_pixels(ImageChops.difference(actual,golden)))
                    maximum=max(max(pixel[:3]) for pixel in differences)
                    # YUV/RGB conversion quantization gives up to a few codes;
                    # layout, line wrapping, collision placement and masks agree.
                    self.assertLessEqual(maximum,4)

    def test_sprite_composition_matches_full_export_compositor_caption_path(self):
        request=self.request();request['style'].update(margin_left=45,margin_right=55,margin_bottom=25)
        request['captions']=[{'text':'Wrapping a longer first caption with a margin.'},{'text':'100% {literal} Türkçe \\N','style':{'size':24,'color':'#40ccff'}}]
        _,sprite=self.full_sprite(request)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);media=root/'media';media.mkdir();path=root/'project.katman'
            for index,color in enumerate(('black','#24588c')):
                Image.new('RGB',(400,240),color).save(media/'background.png')
                source=dict(id='background',type='video',name='Generated background',file='media/background.png',width=400,height=240,media_duration=1)
                clip=dict(id='clip',source_id='background',start=0,**{'in':0},length=1,speed=1,w=400,h=240)
                # The lossless compositor working format is YUV444 after privacy
                # processing. Keep this mask outside the canvas so it changes
                # no pixels, and compare before any delivery chroma subsampling.
                model=dict(width=400,height=240,fps=1,tracks=[dict(kind='video',clips=[clip])],annotations=[dict(kind='redact',start=0,end=1,x=900,y=900,w=2,h=2)],subtitles=[],subtitle_style=request['style'])
                project=dict(format='katman',version=1,sources=[source],studio=model);atomic_json(path,project)
                base=root/f'base{index}.png';golden=root/f'caption{index}.png'
                export_studio(path,base,{'size':[400,240],'pixel_format':'rgba'},model=model,window=(0,.05),frame=True)
                model['subtitles']=[{**row,'start':0,'end':1} for row in request['captions']]
                export_studio(path,golden,{'size':[400,240],'pixel_format':'rgba'},model=model,window=(0,.05),frame=True)
                with Image.open(base) as base_image,Image.open(golden) as golden_image:
                    actual=Image.alpha_composite(base_image.convert('RGBA'),sprite)
                    differences=rgba_pixels(ImageChops.difference(actual,golden_image.convert('RGBA')))
                    self.assertLessEqual(max(max(pixel[:3]) for pixel in differences),4)

    def test_ass_shared_helper_preserves_canonical_timing_styles_and_escaping(self):
        ass=caption_ass([{'start':1.234,'end':2.345,'text':'100% {x} \\N\nŞğ','style':{'color':'#112233','margin_bottom':27}},{'start':0,'end':1,'text':'global'}],{'font':'Noto, Sans','size':30,'color':'#ff0000','margin_left':9},[400,240])
        self.assertIn('PlayResX: 400\nPlayResY: 240\nScaledBorderAndShadow: yes',ass)
        self.assertIn('Style: Caption0,Noto  Sans,30,&H00332211',ass)
        self.assertIn(',2,9,20,27,1',ass)
        self.assertIn('Style: Caption1,Noto  Sans,30,&H000000ff',ass)
        self.assertIn('Dialogue: 0,0:00:01.23,0:00:02.35,Caption0,,0,0,0,,100% \\{x\\} \\\\N\\NŞğ',ass)

    def test_empty_and_invisible_text_produce_single_zero_pixel(self):
        for rows in ([],[{'text':''}],[{'text':'   '}],[{'text':'\n'}]):
            request=self.request();request['captions']=rows
            header,pixels=caption_raster.rasterize(request)
            self.assertEqual(header,{'x':0,'y':0,'width':1,'height':1});self.assertEqual(pixels,bytes(4))

    def test_cli_has_bounded_header_and_exact_packed_payload(self):
        request=self.request();request['captions']=[{'text':'Türkçe {x} 100% \\'}]
        result=subprocess.run([sys.executable,'-m','katman.caption_raster'],input=json.dumps(request).encode(),capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        line,pixels=result.stdout.split(b'\n',1);self.assertLessEqual(len(line)+1,256);header=json.loads(line)
        self.assertEqual(len(pixels),header['width']*header['height']*4);self.assertTrue(any(pixels[3::4]))
        invalid=subprocess.run([sys.executable,'-m','katman.caption_raster'],input=b' '*(caption_raster.MAX_REQUEST_BYTES+1),capture_output=True)
        self.assertNotEqual(invalid.returncode,0);self.assertEqual(invalid.stdout,b'')

    def test_invalid_requests_fail_before_process_or_filesystem_work(self):
        invalid=[{'canvas':[1,240]},{'canvas':[32769,240]},{'width':3841},{'height':2161},{'width':True},{'captions':[{'text':'x\0y'}]},{'captions':[{'text':'x'*65537}]},{'style':{'font':'bad\nfont'}},{'style':{'color':'red'}},{'style':{'size':float('nan')}},{'style':{'margin_bottom':-1}},{'style':{'fontfile':'/tmp/private.ttf'}},{'captions':[{'text':'ok','style':{'font':'bad\nfont'}}]}]
        for changes in invalid:
            request=self.request();request.update(changes)
            with self.subTest(changes=changes),patch.object(caption_raster.subprocess,'run',side_effect=AssertionError('must not launch')):
                with self.assertRaises(ValueError):caption_raster.rasterize(request)

if __name__=='__main__':unittest.main()
