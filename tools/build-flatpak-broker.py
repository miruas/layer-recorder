#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build matching static Flatpak host brokers; never install or grant privileges."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DRM_URL = 'https://dri.freedesktop.org/libdrm/libdrm-2.4.134.tar.xz'
DRM_SHA256 = 'ac5e74d157830eb8bee44c6a6bf3ad49774ef0dd2a72bdad74a8f20308b52a95'
PROXY_URL = 'https://dec05eba.com/snapshot/kms-server-proxy.git.r33.3b056c0.tar.gz'
PROXY_SHA512 = 'f009b3b3b2da6c92b2ca0314c1103072437fdec0b96b0dc3e06a2fb159fbe03bee97f1e180991d5da1a4d25e8763b1ee5723ceffb52734d09a93c3128abd2d7c'


def archive(url, algorithm, digest, destination):
    if not destination.exists():
        with urllib.request.urlopen(url, timeout=90) as response, destination.open('wb') as target:
            shutil.copyfileobj(response, target)
    if hashlib.new(algorithm, destination.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Source checksum mismatch: ' + destination.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--appdir', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=ROOT/'build/flatpak08/broker')
    args = parser.parse_args()
    work = args.workspace.resolve()
    app = args.appdir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    drm_tar, proxy_tar = work/'libdrm.tar.xz', work/'kms-server-proxy.tar.gz'
    archive(DRM_URL, 'sha256', DRM_SHA256, drm_tar)
    archive(PROXY_URL, 'sha512', PROXY_SHA512, proxy_tar)
    drm = work/'libdrm-2.4.134'
    if not drm.exists():
        with tarfile.open(drm_tar) as source: source.extractall(work, filter='data')
    proxy = work/'proxy'
    if not proxy.exists():
        proxy.mkdir()
        with tarfile.open(proxy_tar) as source: source.extractall(proxy, filter='data')
    main_c = next(proxy.rglob('main.c'))
    source_root = main_c.parent
    content = main_c.read_text().replace('com.dec05eba.gpu_screen_recorder','io.github.miruas.Layer')
    if not content.startswith('/* Modified by Layer'):
        content='/* Modified by Layer on 2026-11-11: use the Layer Flatpak helper paths. */\n'+content
    main_c.write_text(content)
    build = work/'drm-build'
    def sdk(*command, cwd=None):
        subprocess.run(['flatpak','build','--filesystem='+str(ROOT),'--filesystem='+str(work),str(app),*map(str,command)],cwd=cwd,check=True)
    if not (build/'build.ninja').exists():
        features = ('intel','radeon','amdgpu','nouveau','vmwgfx','omap','exynos','freedreno','tegra','vc4','etnaviv','cairo-tests','man-pages','valgrind')
        sdk('meson','setup',build,drm,'--default-library=static','--buildtype=release','-Dtests=false',*['-D'+name+'=disabled' for name in features])
    sdk('meson','compile','-C',build,'-j2')
    binaries = work/'bin'
    binaries.mkdir(exist_ok=True)
    sdk('gcc', ROOT/'upstream/gpu-screen-recorder/kms/server/kms_server.c',build/'libdrm.a','-I'+str(drm/'include/drm'),'-I'+str(drm),'-O2','-fstack-protector-all','-static','-static-libgcc','-fno-link-libatomic','-ffile-prefix-map='+str(ROOT)+'=/usr/src/layer','-o',binaries/'gsr-kms-server')
    sdk('sh',source_root/'build.sh',cwd=source_root)
    shutil.copy2(source_root/'kms-server-proxy',binaries/'kms-server-proxy')
    ui = ROOT/'build/native/gsr-ui/source'
    for name in ('gsr-global-hotkeys','gsr-game-tracker','gsr-wayland-bridge'):
        sources = sorted((ui/'tools'/name).glob('*.c'))
        if not sources:
            raise RuntimeError('Build the pinned GSR UI source before its static helpers.')
        sdk('gcc',*sources,'-O2','-static','-static-libgcc','-fno-link-libatomic','-ffile-prefix-map='+str(ROOT)+'=/usr/src/layer','-o',binaries/name)
    print('Static brokers: '+str(binaries))
    print('Corresponding libdrm, proxy and bundled libcap sources: '+str(work))
    print('No helpers have been installed and no capabilities have been granted.')


if __name__ == '__main__': main()
