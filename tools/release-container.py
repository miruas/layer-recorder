#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the ready-to-install Arch release in a clean public container."""
import argparse,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,default=ROOT/'build/arch-release');args=parser.parse_args();args.work.mkdir(parents=True,exist_ok=True);(ROOT/'dist').mkdir(exist_ok=True)
subprocess.run(['docker','build','-f',str(ROOT/'packaging/docker/arch-release.Dockerfile'),'-t','layer-release-arch',str(ROOT)],check=True)
subprocess.run(['docker','run','--rm','-v',str(ROOT)+':/source:ro','-v',str(args.work.resolve())+':/build','-v',str(ROOT/'dist')+':/out','layer-release-arch'],check=True)
