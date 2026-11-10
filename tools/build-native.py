#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the in-tree recorder and pinned optional helpers locally. Never replaces system binaries."""
import argparse,json,shutil,subprocess,os,shlex
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent


def run(*args,cwd=None,env=None):subprocess.run([str(a) for a in args],cwd=cwd,env=env,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('component',choices=['recorder','gpu-screen-recorder','gsr-ui','whisper']);parser.add_argument('--reuse',action='store_true');parser.add_argument('--prefix',type=Path);parser.add_argument('--destdir',type=Path);args=parser.parse_args()
    if args.component=='recorder':
        if not shutil.which('cargo'):parser.error('Install Rust/Cargo to build the capture core.')
        env=os.environ.copy();cargo_home=Path(env.get('CARGO_HOME',Path.home()/'.cargo'));env['RUSTFLAGS']=env.get('RUSTFLAGS','')+' --remap-path-prefix='+str(ROOT)+'=. --remap-path-prefix='+str(cargo_home)+'=/cargo'
        run('cargo','build','--locked','--release','--manifest-path',ROOT/'native/recorder/Cargo.toml',cwd=ROOT/'native/recorder',env=env)
        (ROOT/'bin').mkdir(exist_ok=True);shutil.copy2(ROOT/'native/recorder/target/release/layer-recorderd',ROOT/'bin/layer-recorderd');return
    required=['git']+(['cmake'] if args.component=='whisper' else ['meson','ninja'])
    missing=[tool for tool in required if not shutil.which(tool)]
    if missing:parser.error('Missing build tools: '+', '.join(missing))
    spec=json.loads((ROOT/'packaging/native-lock.json').read_text())[args.component];workspace=ROOT/'build/native'/args.component;source=workspace/'source';build=workspace/'build';workspace.mkdir(parents=True,exist_ok=True)
    if spec.get('source'):source=ROOT/spec['source']
    fresh=not source.exists()
    if not spec.get('source') and source.exists() and not args.reuse:raise SystemExit('Build source already exists. Remove only this local build directory to rebuild: '+str(workspace))
    if not spec.get('source') and fresh:
        run('git','clone','--no-checkout',spec['url'],source);run('git','checkout','--detach',spec['revision'],cwd=source);run('git','submodule','update','--init','--recursive',cwd=source)
    if spec.get('patch') and fresh:run('git','apply',ROOT/spec['patch'],cwd=source)
    mapflag='-ffile-prefix-map='+str(ROOT)+'=.'
    compile_flags=['-Dc_args='+json.dumps(shlex.split(os.environ.get('CFLAGS',''))+[mapflag])]
    if args.component=='gsr-ui':compile_flags+=['-Dcpp_args='+json.dumps(shlex.split(os.environ.get('CXXFLAGS',''))+[mapflag])]
    reconfigure=['--reconfigure'] if (build/'meson-private/coredata.dat').exists() else []
    if args.component=='gpu-screen-recorder':
        run('meson','setup',*reconfigure,build,source,*compile_flags,'--prefix',ROOT/'build/gsr-prefix','--buildtype=release','-Dsystemd=false','-Dcapabilities=false','-Dnvidia_suspend_fix=false')
        run('meson','compile','-C',build,'-j','4');(ROOT/'bin').mkdir(exist_ok=True)
        for binary in ['gpu-screen-recorder','gsr-cli']:shutil.copy2(build/binary,ROOT/'bin'/binary)
        print('The unprivileged engine is ready. A compatible system gsr-kms-server is required for direct KMS capture; the matching broker is also built but not installed or granted capabilities automatically.')
    elif args.component=='whisper':
        run('cmake','-S',source,'-B',build,'-DCMAKE_C_FLAGS='+mapflag,'-DCMAKE_CXX_FLAGS='+mapflag,'-DBUILD_SHARED_LIBS=OFF','-DGGML_NATIVE=OFF','-DWHISPER_BUILD_TESTS=OFF','-DWHISPER_BUILD_EXAMPLES=ON')
        run('cmake','--build',build,'--target','whisper-cli','--parallel','4');dest=ROOT/'speech';dest.mkdir(exist_ok=True);shutil.copy2(build/'bin/whisper-cli',dest/'whisper-cli')
    else:
        dest=args.prefix or ROOT/'overlay';flags=['-Dcapabilities=false','-Ddesktop-files=false']
        run('meson','setup',*reconfigure,build,source,*compile_flags,'--prefix',dest,'--buildtype=release',*flags);run('meson','compile','-C',build,'-j','4')
        run('meson','install','-C',build,*(['--destdir',args.destdir] if args.destdir else []))
    print('Built '+args.component+(' fork based on ' if args.component=='gpu-screen-recorder' else ' from ')+spec['revision'])

if __name__=='__main__':main()
