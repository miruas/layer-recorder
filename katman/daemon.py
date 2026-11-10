# SPDX-License-Identifier: GPL-3.0-only
"""Compatibility entry point: replace this process with the Rust GSR daemon."""
import os,sys
from .common import APP
from .native_runtime import program

def main():
    env=os.environ.copy();env.setdefault('LAYER_PYTHON',sys.executable)
    os.execve(program(),[program(),'--root',str(APP)],env)

if __name__=='__main__':main()
