#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build NumPy/Pillow from pinned sources against the Freedesktop SDK.

Run inside the SDK. No wheel-vendored BLAS, Fortran runtime or image-codec stack
is redistributed. Python package replacement happens only after both locally
built wheels have installed into a private staging directory and passed checks.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SDISTS = {
    'numpy': ('2.5.4', 'numpy-2.5.4.tar.gz', 'https://files.pythonhosted.org/packages/95/b0/c7453d0b6e2073c3264468b106ee1563750cecc910965e67357e3698c83e/numpy-2.5.4.tar.gz', '9a94cf751c9ad8ebaa835bcd3d40dacf8534ad086b88c38029b65123c7999d2a'),
    'pillow': ('12.3.0', 'pillow-12.3.0.tar.gz', 'https://files.pythonhosted.org/packages/1c/3d/bb7fca845737cf9d7dbde16ed1843984665ff2e0a518f5db43e77ec540b9/pillow-12.3.0.tar.gz', '3b8182a766685eaa002637e28b4ec8d6b18819a0c71f579bf0dbaa5830297cce'),
}
# Build tools are not copied into the runtime or corresponding source payload.
BUILD_TOOLS = ['meson-python==0.22.1', 'pyproject-metadata==0.12.1', 'packaging==26.3',
               'pybind11==3.1.0', 'setuptools==83.0.0', 'Cython==3.3.0']
CHECK = r'''
import io, json, os
from pathlib import Path
import numpy as np
from numpy._core._multiarray_umath import __cpu_baseline__
from PIL import Image, ImageDraw, ImageFont, features
assert np.__version__ == "2.5.4"
assert not __cpu_baseline__, "NumPy must have a generic C baseline, not X86_V2"
assert np.linalg.norm(np.array([3., 4.])) == 5.
assert np.allclose(np.linalg.solve(np.array([[2.,0.],[0.,4.]]),np.array([4.,8.])),[2.,2.])
assert not (Path(np.__file__).parent.parent / "numpy.libs").exists()
assert not (Path(Image.__file__).parent.parent / "pillow.libs").exists()
assert features.check("freetype2") and features.check("zlib") and features.check("jpg")
for feature in ("webp", "avif", "jpg_2000", "raqm", "libtiff", "littlecms2", "xcb", "libimagequant"):
    assert not features.check(feature), feature
image=Image.new("RGBA",(128,48),(0,0,0,0));draw=ImageDraw.Draw(image)
font=ImageFont.truetype(os.environ['LAYER_TEST_FONT'],14)
draw.text((2,2),"Layer",font=font,fill="white")
output=io.BytesIO();image.save(output,format="PNG");assert Image.open(io.BytesIO(output.getvalue())).size==(128,48)
print(json.dumps({"numpy":np.__version__,"cpu_baseline":__cpu_baseline__,"pillow":Image.__version__,"freetype":features.version("freetype2"),"zlib":features.version("zlib"),"jpeg":features.version("jpg")}))
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    runtime = Path('/app/lib/layer-recorder/python')
    if not Path('/usr/lib/sdk').is_dir() or not runtime.parent.is_dir():
        raise RuntimeError('Run this helper inside the initialized Freedesktop SDK build directory.')
    work=ROOT/'build/flatpak08/python-source';work.mkdir(parents=True,exist_ok=True)
    cache=ROOT/'build/flatpak08/source-sdists';cache.mkdir(parents=True,exist_ok=True)
    tools=work/'build-tools';wheels=work/'wheels';wheels.mkdir(exist_ok=True)
    jobs=max(1,int(os.environ.get('LAYER_JOBS','4')))
    env=os.environ.copy()
    # Source defaults never inherit a developer's -march=native or custom BLAS.
    remap='-ffile-prefix-map='+str(ROOT)+'=/usr/src/layer'
    env.update(CFLAGS='-O2 -fPIC '+remap,CXXFLAGS='-O2 -fPIC '+remap,
               PYTHONPATH=str(tools),PYTHONDONTWRITEBYTECODE='1',PIP_DISABLE_PIP_VERSION_CHECK='1',
               MAX_CONCURRENCY=str(jobs),CMAKE_BUILD_PARALLEL_LEVEL=str(jobs))
    for key in ('BLAS','LAPACK','NPY_BLAS_ORDER','NPY_LAPACK_ORDER','CMAKE_ARGS'):
        env.pop(key,None)
    def run(*command,environment=env,**kwargs):
        subprocess.run([str(item) for item in command],env=environment,check=True,**kwargs)
    tool_stamp=work/'build-tools.json'
    if not tool_stamp.exists() or json.loads(tool_stamp.read_text())!=BUILD_TOOLS:
        if tools.exists():shutil.rmtree(tools)
        run(sys.executable,'-m','pip','install','--no-deps','--target',tools,*BUILD_TOOLS)
        tool_stamp.write_text(json.dumps(BUILD_TOOLS))
    flags={
      'numpy':['-Csetup-args=-Dblas=none','-Csetup-args=-Dlapack=none','-Csetup-args=-Dallow-noblas=true',
               '-Csetup-args=-Dcpu-baseline=none','-Csetup-args=-Dcpu-baseline-detect=disabled',
               '-Csetup-args=-Dcpu-dispatch=X86_V2,X86_V3' if platform.machine()=='x86_64' else '-Csetup-args=-Dcpu-dispatch=none',
               '-Csetup-args=-Ddisable-svml=true','-Csetup-args=-Ddisable-highway=true','-Csetup-args=-Ddisable-intel-sort=true',
               '-Ccompile-args=-j'+str(jobs)],
      'pillow':['-Cparallel='+str(jobs),'-Cfreetype=enable','-Czlib=enable','-Cjpeg=enable',
                *['-C'+feature+'=disable' for feature in ('avif','webp','jpeg2000','raqm','imagequant','xcb','tiff','lcms')]],
    }
    wheel_paths=[];provenance={}
    for name,(version,filename,url,expected) in SDISTS.items():
        archive=cache/filename
        if not archive.exists():
            with urllib.request.urlopen(url,timeout=90) as response,archive.open('wb') as output:
                shutil.copyfileobj(response,output)
        if sha(archive)!=expected:raise RuntimeError('Python source checksum mismatch: '+name)
        fingerprint={'source_sha256':expected,'python':sys.version,'platform':platform.machine(),
                     'source_date_epoch':env.get('SOURCE_DATE_EPOCH'),
                     'sdk_commit':env.get('LAYER_SDK_COMMIT'),
                     'tools':BUILD_TOOLS,'flags':flags[name],'cflags':env['CFLAGS'],
                     'sdk_compiler':subprocess.check_output(['cc','--version'],text=True).splitlines()[0]}
        stamp=work/(name+'-wheel.json')
        saved=json.loads(stamp.read_text()) if stamp.exists() else {}
        candidate=wheels/saved.get('wheel','missing')
        if saved.get('fingerprint')!=fingerprint or not candidate.is_file() or sha(candidate)!=saved.get('wheel_sha256'):
            for old in wheels.glob(name+'-'+version+'-*.whl'):old.unlink()
            run(sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','--no-cache-dir','--wheel-dir',wheels,*flags[name],archive)
            candidates=list(wheels.glob(name+'-'+version+'-*.whl'))
            if len(candidates)!=1:raise RuntimeError('Expected exactly one source-built '+name+' wheel.')
            candidate=candidates[0]
            saved={'fingerprint':fingerprint,'wheel':candidate.name,'wheel_sha256':sha(candidate),'source_url':url}
            stamp.write_text(json.dumps(saved,indent=2,sort_keys=True)+'\n')
        wheel_paths.append(candidate);provenance[name]=saved
    staging=work/'runtime-stage'
    if staging.exists():shutil.rmtree(staging)
    run(sys.executable,'-m','pip','install','--no-index','--no-deps','--no-compile','--target',staging,*wheel_paths)
    check_env=env.copy();check_env['PYTHONPATH']=str(staging)
    # Use fontconfig to locate an installed SDK font without baking a host path
    # into runtime code or depending on a particular distribution's font layout.
    font=subprocess.check_output(['fc-match','-f','%{file}','sans'],text=True).strip()
    check=CHECK
    check_env['LAYER_TEST_FONT']=font
    run(sys.executable,'-c',check,environment=check_env)
    # No bundled native helper libraries are expected from this build policy.
    for directory in staging.glob('*.libs'):
        if directory.is_dir():raise RuntimeError('Unexpected wheel-vendored libraries: '+directory.name)
    backup=work/'runtime-backup'
    if backup.exists():shutil.rmtree(backup)
    backup.mkdir()
    runtime.mkdir(parents=True,exist_ok=True)
    old=[item for item in runtime.iterdir() if item.name in ('numpy','numpy.libs','PIL','pillow.libs') or item.name.startswith(('numpy-','pillow-'))]
    installed=[]
    try:
        for item in old:shutil.move(item,backup/item.name)
        for item in staging.iterdir():
            if item.name=='bin':continue  # NumPy CLI tools are not application runtime inputs.
            destination=runtime/item.name
            if destination.exists():raise RuntimeError('Unexpected replacement collision: '+item.name)
            shutil.move(item,destination);installed.append(destination)
        live_env=env.copy();live_env['PYTHONPATH']=str(runtime);live_env['LAYER_TEST_FONT']=font
        run(sys.executable,'-c',check,environment=live_env)
    except BaseException:
        for item in installed:
            if item.is_dir():shutil.rmtree(item)
            elif item.exists():item.unlink()
        for item in backup.iterdir():shutil.move(item,runtime/item.name)
        raise
    shutil.rmtree(backup)
    metadata=Path('/app/share/doc/layer-recorder');metadata.mkdir(parents=True,exist_ok=True)
    (metadata/'PYTHON_SOURCE_BUILDS.json').write_text(json.dumps(provenance,indent=2,sort_keys=True)+'\n')
    print('Installed source-built NumPy/Pillow; no bundled BLAS, Fortran runtime or image-codec libraries.')


if __name__=='__main__':main()
