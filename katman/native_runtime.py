# SPDX-License-Identifier: GPL-3.0-only
"""Locate the compiled capture core without a silent Python capture fallback."""
import os
from .common import APP

def program():
    binary=APP/'bin/layer-recorderd'
    if not binary.is_file() or not os.access(binary,os.X_OK):raise RuntimeError('Build the Rust capture core with python tools/build-native.py recorder, or install the compiled Layer package.')
    return str(binary)
