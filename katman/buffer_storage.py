# SPDX-License-Identifier: GPL-3.0-only
"""Compatibility RAM-allocation API; filesystem checks and allocation run in Rust."""
import json,subprocess
from pathlib import Path
from .native_runtime import program
MIN_FREE=512*1024*1024

def create_ram_buffer():
    p=subprocess.run([program(),'ram-create'],capture_output=True,text=True,timeout=10)
    if p.returncode:raise RuntimeError(p.stderr.strip())
    return Path(json.loads(p.stdout))
