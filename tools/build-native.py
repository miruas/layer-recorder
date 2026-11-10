#!/usr/bin/env python3
"""Build pinned optional helpers locally. Never replaces system binaries."""
import argparse,json,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent


def run(*args,cwd=None):subprocess.run([str(a) for a in args],cwd=cwd,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('component',choices=['wf-recorder','gsr-ui','whisper']);args=parser.parse_args()
    required=['git']+(['cmake'] if args.component=='whisper' else ['meson','ninja'])
    missing=[tool for tool in required if not shutil.which(tool)]
    if missing:parser.error('Missing build tools: '+', '.join(missing))
    spec=json.loads((ROOT/'packaging/native-lock.json').read_text())[args.component];workspace=ROOT/'build/native'/args.component;source=workspace/'source';build=workspace/'build';workspace.mkdir(parents=True,exist_ok=True)
    if source.exists():raise SystemExit('Build source already exists. Remove only this local build directory to rebuild: '+str(workspace))
    run('git','clone','--no-checkout',spec['url'],source);run('git','checkout','--detach',spec['revision'],cwd=source);run('git','submodule','update','--init','--recursive',cwd=source)
    if spec.get('patch'):run('git','apply',ROOT/spec['patch'],cwd=source)
    if args.component=='whisper':
        run('cmake','-S',source,'-B',build,'-DBUILD_SHARED_LIBS=OFF','-DGGML_NATIVE=OFF','-DWHISPER_BUILD_TESTS=OFF','-DWHISPER_BUILD_EXAMPLES=ON')
        run('cmake','--build',build,'--target','whisper-cli','--parallel','4');dest=ROOT/'speech';dest.mkdir(exist_ok=True);shutil.copy2(build/'bin/whisper-cli',dest/'whisper-cli')
    else:
        dest=ROOT/('overlay' if args.component=='gsr-ui' else 'build/wf-prefix');flags=['-Dcapabilities=false','-Ddesktop-files=false'] if args.component=='gsr-ui' else ['-Dpulse=disabled','-Dpipewire=disabled']
        run('meson','setup',build,source,'--prefix',dest,'--buildtype=release',*flags);run('meson','compile','-C',build,'-j','4')
        if args.component=='gsr-ui':run('meson','install','-C',build)
        else:
            (ROOT/'bin').mkdir(exist_ok=True);shutil.copy2(build/'wf-recorder',ROOT/'bin/katman-wf-recorder')
    print('Built '+args.component+' from '+spec['revision'])

if __name__=='__main__':main()
