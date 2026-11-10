#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the in-tree recorder and pinned optional helpers locally. Never replaces system binaries."""
import argparse,json,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent


def run(*args,cwd=None):subprocess.run([str(a) for a in args],cwd=cwd,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('component',choices=['gpu-screen-recorder','wf-recorder','gsr-ui','whisper']);args=parser.parse_args()
    required=['git']+(['cmake'] if args.component=='whisper' else ['meson','ninja'])
    missing=[tool for tool in required if not shutil.which(tool)]
    if missing:parser.error('Missing build tools: '+', '.join(missing))
    spec=json.loads((ROOT/'packaging/native-lock.json').read_text())[args.component];workspace=ROOT/'build/native'/args.component;source=workspace/'source';build=workspace/'build';workspace.mkdir(parents=True,exist_ok=True)
    if spec.get('source'):source=ROOT/spec['source']
    if not spec.get('source') and source.exists():raise SystemExit('Build source already exists. Remove only this local build directory to rebuild: '+str(workspace))
    if not spec.get('source'):
        run('git','clone','--no-checkout',spec['url'],source);run('git','checkout','--detach',spec['revision'],cwd=source);run('git','submodule','update','--init','--recursive',cwd=source)
    if spec.get('patch'):run('git','apply',ROOT/spec['patch'],cwd=source)
    if args.component=='gpu-screen-recorder':
        run('meson','setup',build,source,'--prefix',ROOT/'build/gsr-prefix','--buildtype=release','-Dsystemd=false','-Dcapabilities=false','-Dnvidia_suspend_fix=false')
        run('meson','compile','-C',build,'-j','4');(ROOT/'bin').mkdir(exist_ok=True)
        for binary in ['gpu-screen-recorder','gsr-cli']:shutil.copy2(build/binary,ROOT/'bin'/binary)
        print('The unprivileged engine is ready. A compatible system gsr-kms-server is required for direct KMS capture; the matching broker is also built but not installed or granted capabilities automatically.')
    elif args.component=='whisper':
        run('cmake','-S',source,'-B',build,'-DBUILD_SHARED_LIBS=OFF','-DGGML_NATIVE=OFF','-DWHISPER_BUILD_TESTS=OFF','-DWHISPER_BUILD_EXAMPLES=ON')
        run('cmake','--build',build,'--target','whisper-cli','--parallel','4');dest=ROOT/'speech';dest.mkdir(exist_ok=True);shutil.copy2(build/'bin/whisper-cli',dest/'whisper-cli')
    else:
        dest=ROOT/('overlay' if args.component=='gsr-ui' else 'build/wf-prefix');flags=['-Dcapabilities=false','-Ddesktop-files=false'] if args.component=='gsr-ui' else ['-Dpulse=disabled','-Dpipewire=disabled']
        run('meson','setup',build,source,'--prefix',dest,'--buildtype=release',*flags);run('meson','compile','-C',build,'-j','4')
        if args.component=='gsr-ui':run('meson','install','-C',build)
        else:
            (ROOT/'bin').mkdir(exist_ok=True);shutil.copy2(build/'wf-recorder',ROOT/'bin/katman-wf-recorder')
    print('Built '+args.component+(' fork based on ' if args.component=='gpu-screen-recorder' else ' from ')+spec['revision'])

if __name__=='__main__':main()
