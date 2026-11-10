# SPDX-License-Identifier: GPL-3.0-only
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from katman.buffer_storage import create_ram_buffer
from katman.native_runtime import program


def mount_type(path):
    target=Path(path).resolve()
    best=None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1)
        mount=Path(left.split()[4].replace('\\040',' '))
        if target==mount or target.is_relative_to(mount):
            if best is None or len(str(mount))>len(str(best[0])):best=(mount,right.split()[0])
    if best is None:raise AssertionError('No mount point found for '+str(target))
    return best[1]


class RamReplay(unittest.TestCase):
    def test_real_allocator_creates_private_tmpfs_directory(self):
        try:program()
        except RuntimeError as error:self.skipTest(str(error))
        root=create_ram_buffer()
        try:
            self.assertTrue(root.is_dir())
            self.assertTrue(root.is_relative_to(Path('/dev/shm')))
            self.assertEqual(root.parent,Path('/dev/shm')/f'layer-{os.getuid()}')
            self.assertEqual(mount_type(root),'tmpfs')
            self.assertEqual(root.stat().st_dev,os.stat('/dev/shm').st_dev)
            self.assertEqual(root.stat().st_uid,os.getuid())
            self.assertEqual(root.parent.stat().st_uid,os.getuid())
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode),0o700)
        finally:shutil.rmtree(root)
        self.assertFalse(root.exists())

    def test_wrapper_forwards_allocator_stderr(self):
        error=subprocess.CompletedProcess(['synthetic-layer-recorderd','ram-create'],1,'','synthetic allocator failure')
        with patch('katman.buffer_storage.program',return_value='synthetic-layer-recorderd'),patch('katman.buffer_storage.subprocess.run',return_value=error) as run:
            with self.assertRaises(RuntimeError) as caught:create_ram_buffer()
        self.assertEqual(str(caught.exception),'synthetic allocator failure')
        run.assert_called_once_with(['synthetic-layer-recorderd','ram-create'],capture_output=True,text=True,timeout=10)
