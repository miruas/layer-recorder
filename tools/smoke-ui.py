#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise native Rust startup/close with isolated slow-I/O regressions."""
import argparse
import subprocess
import sys
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--screenshots',type=Path)
args=parser.parse_args()
root=Path(__file__).resolve().parent.parent
output=args.screenshots or root/'build/ui-smoke'
subprocess.run([sys.executable,str(root/'tools/test-native-startup.py'),str(root/'bin/layer-studio'),'--output',str(output)],check=True)
