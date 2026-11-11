# SPDX-License-Identifier: GPL-3.0-only
"""Launch the single native archive/editor window without importing Qt."""
import os
import sys
from .common import APP


def main(path=None):
    binary = APP / 'bin/layer-studio'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('The native editor is missing. Install the complete Layer package or run tools/build-native.py editor.')
    command = [str(binary), '--root', str(APP)]
    if path:
        command += ['--project', str(path)]
    environment = os.environ.copy()
    environment.setdefault('LAYER_PYTHON', sys.executable)
    environment.update(LANG="C.UTF-8",LC_ALL="C.UTF-8")
    os.execve(str(binary), command, environment)
