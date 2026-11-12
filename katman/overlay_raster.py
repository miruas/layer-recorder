# SPDX-License-Identifier: GPL-3.0-only
"""Rasterize one independent white title sprite; never reads source media.

CLI: one bounded JSON request on stdin; exactly width*height*4 RGBA bytes on
stdout. Position, color and opacity deliberately belong to the GPU compositor.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from .studio_render import font_file, quote

MAX_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024


def validate(request):
    if not isinstance(request, dict):
        raise ValueError('Expected a text sprite request.')
    width, height, size = (request.get(name) for name in ('width', 'height', 'size_px'))
    if any(type(value) is not int for value in (width, height, size)):
        raise ValueError('Sprite dimensions and font size must be integers.')
    if not 4 <= width <= 4096 or not 4 <= height <= 2048 or width * height * 4 > MAX_BYTES:
        raise ValueError('Text sprite exceeds its size limit.')
    if not 1 <= size <= 1000:
        raise ValueError('Font size must be between 1 and 1000 pixels.')
    text, font = request.get('text'), request.get('font')
    if not isinstance(text, str) or len(text.encode('utf-8')) > 64 * 1024 or '\0' in text:
        raise ValueError('Text must contain at most 64 KiB and no null characters.')
    if not isinstance(font, str) or not font.strip() or len(font.encode('utf-8')) > 1024 or any(ord(c) < 32 for c in font):
        raise ValueError('Invalid font family.')
    return width, height, size, text, font


def rasterize(request):
    width, height, size, text, font = validate(request)
    resolved_font = font_file(font)
    with tempfile.TemporaryDirectory(prefix='layer-text-') as directory:
        text_file = Path(directory) / 'text.txt'
        text_file.write_text(text, encoding='utf-8')
        # export uses the same Fontconfig resolution, textfile and 1px outline.
        # expansion=none keeps arbitrary user text literal (%, backslashes, etc.).
        # No interpolation of user text or family names into the filter grammar.
        draw = (f"drawtext=fontfile='{quote(resolved_font)}':textfile='{quote(text_file)}':"
                f"fontsize={size}:fontcolor=white:borderw=1:bordercolor=black:"
                "expansion=none:x=2:y=2")
        result = subprocess.run([
            'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin',
            '-f', 'lavfi', '-i', f'color=c=black@0:s={width}x{height}:r=1,format=rgba',
            '-vf', draw, '-frames:v', '1', '-threads', '1',
            '-c:v', 'rawvideo', '-pix_fmt', 'rgba', '-f', 'rawvideo', 'pipe:1',
        ], capture_output=True, timeout=8, check=False)
    if result.returncode:
        raise RuntimeError('Text rasterization failed: ' + result.stderr.decode('utf-8', errors='replace')[-2000:])
    if len(result.stdout) != width * height * 4:
        raise RuntimeError('Text rasterizer returned an incomplete sprite.')
    return result.stdout


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('Text sprite request is too large.')
        pixels = rasterize(json.loads(raw))
        sys.stdout.buffer.write(pixels)
        sys.stdout.buffer.flush()
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
