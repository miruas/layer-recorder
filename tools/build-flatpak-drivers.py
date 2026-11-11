#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the pinned legacy Intel VA-API driver inside the Freedesktop SDK."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
URL='https://github.com/irql-notlessorequal/intel-vaapi-driver/archive/refs/tags/2.4.5.tar.gz'
SHA256='f89f77bc46ec6f46392c1b90b9bf34d09f908bf33c9d16d385897b1fe282bacc'


def main():
    if platform.machine() != 'x86_64': return
    work=ROOT/'build/flatpak08/intel-vaapi-driver';work.mkdir(parents=True,exist_ok=True)
    archive=work/'source.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(URL,timeout=90) as response,archive.open('wb') as target:shutil.copyfileobj(response,target)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA256:raise RuntimeError('Intel VA-API source checksum mismatch.')
    source=work/'intel-vaapi-driver-2.4.5'
    if not source.exists():
        with tarfile.open(archive) as tar:tar.extractall(work,filter='data')
    # The runtime omits the old Intel bufmgr, just as it omits i965. Build
    # its matching libdrm component from the same pinned source as the broker.
    drm=ROOT/'build/flatpak08/broker/libdrm-2.4.134'
    if not drm.is_dir():
        drm_archive=work/'libdrm.tar.xz'
        with urllib.request.urlopen('https://dri.freedesktop.org/libdrm/libdrm-2.4.134.tar.xz',timeout=90) as response,drm_archive.open('wb') as target:shutil.copyfileobj(response,target)
        if hashlib.sha256(drm_archive.read_bytes()).hexdigest()!='ac5e74d157830eb8bee44c6a6bf3ad49774ef0dd2a72bdad74a8f20308b52a95':raise RuntimeError('libdrm source checksum mismatch.')
        drm.parent.mkdir(parents=True,exist_ok=True)
        with tarfile.open(drm_archive) as tar:tar.extractall(drm.parent,filter='data')
    drm_build=work/'drm-build'
    if not (drm_build/'build.ninja').exists():
        features=('radeon','amdgpu','nouveau','vmwgfx','omap','exynos','freedreno','tegra','vc4','etnaviv','cairo-tests','man-pages','valgrind')
        subprocess.run(['meson','setup',str(drm_build),str(drm),'--prefix=/app','--libdir=lib','--default-library=static','--buildtype=release','-Dintel=enabled','-Dtests=false',*['-D'+name+'=disabled' for name in features]],check=True)
    subprocess.run(['meson','compile','-C',str(drm_build),'-j',os.environ.get('LAYER_JOBS','2')],check=True)
    subprocess.run(['meson','install','-C',str(drm_build)],check=True)
    build=work/'build'
    if not (build/'build.ninja').exists():
        subprocess.run(['meson','setup',str(build),str(source),'--prefix=/app','--buildtype=release','-Ddriverdir=/app/lib/dri','-Dwith_x11=no','-Dwith_wayland_drm=no','-Dc_args=-ffile-prefix-map='+str(ROOT)+'=/usr/src/layer'],check=True)
    subprocess.run(['meson','compile','-C',str(build),'-j',os.environ.get('LAYER_JOBS','2')],check=True)
    subprocess.run(['meson','install','-C',str(build)],check=True)
    licenses=Path('/app/share/licenses/io.github.miruas.Layer/intel-vaapi-driver');licenses.mkdir(parents=True,exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and path.name.upper().startswith(('COPYING','LICENSE','NOTICE','COPYRIGHT')):shutil.copy2(path,licenses/path.name)


if __name__=='__main__':main()
