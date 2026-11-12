# SPDX-License-Identifier: GPL-3.0-only
"""Run the recorder's actual C sizing code without opening capture or a GPU."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class NativeCaptureResolution(unittest.TestCase):
    def test_capture_bounds_do_not_change_overlay_scaling(self):
        if not shutil.which('cc') or not shutil.which('pkg-config'):
            self.skipTest('C compiler and development headers are required')
        deps = subprocess.run(['pkg-config', '--cflags', 'libdrm', 'libavcodec', 'libva', 'xrandr'], capture_output=True, text=True)
        if deps.returncode:
            self.skipTest('Recorder development headers are not installed')
        root = Path(__file__).resolve().parents[1] / 'upstream/gpu-screen-recorder'
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            harness = tmp/'bounds.c'
            harness.write_text(r'''
#include "utils.h"
#include <assert.h>
static void check(int w, int h, int max_w, int max_h, int expected_w, int expected_h) {
    vec2i result = limit_capture_resolution((vec2i){w,h}, (vec2i){max_w,max_h});
    assert(result.x == expected_w && result.y == expected_h);
    assert(result.x <= w && result.y <= h);
    assert(result.x <= max_w && result.y <= max_h);
}
int main(void) {
    check(1280,720,3840,2160,1280,720);
    check(1920,1080,1920,1080,1920,1080);
    check(2560,1600,1920,1080,1728,1080);
    check(1080,1920,1920,1080,606,1080);
    check(3440,1440,1920,1080,1920,802);
    check(1279,719,3840,2160,1278,718);
    check(1920,1080,1919,1079,1918,1078);
    vec2i native = limit_capture_resolution((vec2i){1280,720}, (vec2i){0,0});
    assert(native.x == 1280 && native.y == 720);
    vec2i unknown = limit_capture_resolution((vec2i){0,0}, (vec2i){1920,1080});
    assert(unknown.x == 0 && unknown.y == 0);
    vec2i overlay = scale_keep_aspect_ratio((vec2i){640,360}, (vec2i){1920,1080});
    assert(overlay.x == 1920 && overlay.y == 1080);
    return 0;
}
''')
            import shlex
            result = subprocess.run(['cc', '-std=gnu11', '-ffunction-sections', '-fdata-sections',
                                     '-Wl,--gc-sections', '-I'+str(root/'include'), *shlex.split(deps.stdout),
                                     str(harness), str(root/'src/utils.c'), '-o', str(tmp/'bounds')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(tmp/'bounds')], check=True, timeout=10)
