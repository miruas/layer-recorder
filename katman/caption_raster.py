# SPDX-License-Identifier: GPL-3.0-only
"""Render active captions as a cropped transparent libass sprite, without media.

One bounded JSON input; stdout contains a JSON header line followed by packed,
straight-alpha RGBA. Header coordinates are in the requested raster pixels.
"""
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from PIL import Image
from .studio_render import caption_ass, quote

MAX_REQUEST_BYTES=512*1024
MAX_BYTES=32*1024*1024
MAX_TEXT_BYTES=64*1024
MAX_TOTAL_TEXT_BYTES=256*1024
STYLE_KEYS={'font','size','color','margin_left','margin_right','margin_bottom'}


def _style(style):
    if not isinstance(style,dict) or not set(style).issubset(STYLE_KEYS):
        raise ValueError('Unsupported caption style.')
    if 'font' in style:
        font=style['font']
        if not isinstance(font,str) or not font.strip() or len(font.encode('utf8'))>1024 or any(ord(c)<32 for c in font):
            raise ValueError('Invalid caption font family.')
    if 'color' in style and (not isinstance(style['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',style['color'])):
        raise ValueError('Caption color must be #RRGGBB.')
    for key in ('size','margin_left','margin_right','margin_bottom'):
        if key not in style:continue
        value=style[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('Caption size and margins must be finite numbers.')
        minimum,maximum=(1,2048) if key=='size' else (0,32768)
        if not minimum<=value<=maximum:raise ValueError('Caption size or margin is out of range.')
    return dict(style)


def validate(request):
    if not isinstance(request,dict):raise ValueError('Expected a caption sprite request.')
    canvas=request.get('canvas');width=request.get('width');height=request.get('height')
    if not isinstance(canvas,(list,tuple)) or len(canvas)!=2 or any(type(n) is not int or not 2<=n<=32768 for n in canvas):
        raise ValueError('Caption canvas dimensions must be between 2 and 32768.')
    if type(width) is not int or type(height) is not int or not 2<=width<=3840 or not 2<=height<=2160 or width*height*4>MAX_BYTES:
        raise ValueError('Caption sprite exceeds its size limit.')
    style=_style(request.get('style',{}));captions=request.get('captions')
    if not isinstance(captions,list) or len(captions)>256:raise ValueError('Caption sprite requires at most 256 active captions.')
    normalized=[];total=0
    for row in captions:
        if not isinstance(row,dict) or not isinstance(row.get('text'),str):raise ValueError('Caption text must be a string.')
        text=row['text'];length=len(text.encode('utf8'));total+=length
        if length>MAX_TEXT_BYTES or total>MAX_TOTAL_TEXT_BYTES or '\0' in text:raise ValueError('Caption text exceeds its limit or contains a null character.')
        normalized.append({'text':text,'style':_style(row.get('style',{})),'start':0,'end':1})
    return tuple(canvas),width,height,normalized,style


def rasterize(request):
    canvas,width,height,captions,style=validate(request)
    if not captions or not any(row['text'] for row in captions):return {'x':0,'y':0,'width':1,'height':1},bytes(4)
    with tempfile.TemporaryDirectory(prefix='layer-caption-') as directory:
        file=Path(directory)/'captions.ass';file.write_text(caption_ass(captions,style,canvas),encoding='utf8')
        result=subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-f','lavfi','-i',f'color=c=black@0:s={width}x{height}:r=1,format=yuva444p',
            '-vf',f"ass=filename='{quote(file)}':alpha=1",'-frames:v','1','-threads','1','-c:v','rawvideo','-pix_fmt','rgba','-f','rawvideo','pipe:1'],capture_output=True,timeout=10,check=False)
    if result.returncode:raise RuntimeError('Caption rasterization failed: '+result.stderr.decode('utf8',errors='replace')[-2000:])
    if len(result.stdout)!=width*height*4:raise RuntimeError('Caption rasterizer returned an incomplete sprite.')
    image=Image.frombytes('RGBA',(width,height),result.stdout);box=image.getchannel('A').getbbox()
    if box is None:return {'x':0,'y':0,'width':1,'height':1},bytes(4)
    x,y,right,bottom=box
    return {'x':x,'y':y,'width':right-x,'height':bottom-y},image.crop(box).tobytes()


def main():
    try:
        raw=sys.stdin.buffer.read(MAX_REQUEST_BYTES+1)
        if len(raw)>MAX_REQUEST_BYTES:raise ValueError('Caption sprite request is too large.')
        header,pixels=rasterize(json.loads(raw));line=json.dumps(header,separators=(',',':')).encode('ascii')+b'\n'
        if len(line)>256:raise RuntimeError('Caption header is too large.')
        sys.stdout.buffer.write(line);sys.stdout.buffer.write(pixels);sys.stdout.buffer.flush()
        return 0
    except (ValueError,TypeError,OSError,RuntimeError,subprocess.SubprocessError) as error:
        print(str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
