#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the editor's full FFmpeg CLI inside the fixed SDK, including text and stabilization."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
INPUTS={
 'libass':('https://github.com/libass/libass/releases/download/0.17.5/libass-0.17.5.tar.xz','2dca25c0e0c837ddf00b52011b3f82cac1e4ddd3ad018227806b0c2288864acc','libass-0.17.5'),
 'x265':('https://download.videolan.org/pub/videolan/x265/x265_4.1.tar.gz','a31699c6a89806b74b0151e5e6a7df65de4b49050482fe5ebf8a4379d7af8f29','x265_4.1/source'),
 'vidstab':('https://github.com/georgmartius/vid.stab/archive/refs/tags/v1.1.1.tar.gz','9001b6df73933555e56deac19a0f225aae152abbc0e97dc70034814a1943f3d4','vid.stab-1.1.1'),
 'aom':('https://aomedia.googlesource.com/aom/+archive/refs/tags/v3.15.3.tar.gz','6b2b5f61565055677b0dd296c245b4caec286a2111e8bdf6a6ef02d73c58f773','aom-3.15.3-source'),
}


def main():
 work=ROOT/'build/flatpak08/renderer';work.mkdir(parents=True,exist_ok=True)
 prefix=work/'prefix';prefix.mkdir(exist_ok=True)
 environment=os.environ.copy()
 jobs=str(max(1,int(environment.get('LAYER_JOBS','4'))))
 core=Path(os.environ.get('LAYER_GSR_STACK',str(ROOT/'build/flatpak08/gsr-build/subprojects/ffmpeg-9.0/install')))
 if not core.is_dir():raise RuntimeError('Build the static GSR FFmpeg dependency stack first.')
 environment['PKG_CONFIG_PATH']=str(prefix/'lib/pkgconfig')+':'+str(core/'lib/pkgconfig')+':'+environment.get('PKG_CONFIG_PATH','')
 environment['CFLAGS']=environment.get('CFLAGS','')+' -ffile-prefix-map='+str(ROOT)+'=/usr/src/layer'
 environment['CXXFLAGS']=environment.get('CXXFLAGS','')+' -ffile-prefix-map='+str(ROOT)+'=/usr/src/layer'
 deps_changed=False
 def run(*command,cwd=None):subprocess.run(list(map(str,command)),cwd=cwd,env=environment,check=True)
 for name,(url,digest,directory) in INPUTS.items():
  archive=work/(name+'-'+digest[:12]+'.tar.gz')
  if not archive.exists():
   with urllib.request.urlopen(url,timeout=90) as response,archive.open('wb') as target:shutil.copyfileobj(response,target)
  if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest:raise RuntimeError('Renderer dependency checksum mismatch: '+name)
  source=work/directory
  if not source.exists():
   target=source if name=='aom' else work;target.mkdir(exist_ok=True)
   with tarfile.open(archive) as tar:tar.extractall(target,filter='data')
  if name=='x265':
   cmake=source/'CMakeLists.txt';content=cmake.read_text()
   updated=content.replace('cmake_policy(SET CMP0025 OLD)','cmake_policy(SET CMP0025 NEW)').replace('cmake_policy(SET CMP0054 OLD)','cmake_policy(SET CMP0054 NEW)')
   if updated!=content:cmake.write_text('# Modified by Layer on 2026-11-11: support SDK CMake 4 policies.\n'+updated)
  if name=='aom':
   cmake=source/'cmake/aom_optimization.cmake';content=cmake.read_text()
   marker='  if(NOT "${nasm_helptext}" MATCHES "-Ox")'
   if 'nasm_optimization_helptext' not in content:
    extra='  execute_process(COMMAND ${CMAKE_ASM_NASM_COMPILER} -h -O OUTPUT_VARIABLE nasm_optimization_helptext)\n  string(APPEND nasm_helptext "${nasm_optimization_helptext}")\n'
    cmake.write_text('# Modified by Layer on 2026-11-11: detect NASM 3 help layout.\n'+content.replace(marker,extra+marker))
  build=work/(name+'-'+digest[:12]+'-build')
  stamp=build/'layer-complete'
  flags=['-DCMAKE_INSTALL_PREFIX='+str(prefix),'-DCMAKE_INSTALL_LIBDIR=lib','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_POLICY_VERSION_MINIMUM=3.5','-DCMAKE_POSITION_INDEPENDENT_CODE=ON','-DBUILD_SHARED_LIBS=OFF','-DCMAKE_C_STANDARD=11']
  if name=='x265':flags+=['-DENABLE_SHARED=OFF','-DENABLE_CLI=OFF','-DENABLE_LIBNUMA=OFF']
  elif name=='aom':flags+=['-DENABLE_TESTS=OFF','-DENABLE_EXAMPLES=OFF','-DENABLE_TOOLS=OFF','-DENABLE_DOCS=OFF','-DENABLE_NASM=ON','-DCMAKE_C_STANDARD=11']
  else:flags+=['-DUSE_OMP=OFF']
  if name=='libass':flags=['--prefix='+str(prefix),'--libdir='+str(prefix/'lib'),'--disable-shared','--enable-static','--with-pic','--disable-libunibreak','--disable-test']
  identity=json.dumps({'archive':digest,'flags':flags,'compiler_flags':[environment['CFLAGS'],environment['CXXFLAGS']],'patch':hashlib.sha256((source/('configure' if name=='libass' else 'CMakeLists.txt' if name!='aom' else 'cmake/aom_optimization.cmake')).read_bytes()).hexdigest()},sort_keys=True)
  if not stamp.exists() or stamp.read_text()!=identity:
   deps_changed=True
   if name=='libass':
    build.mkdir(exist_ok=True)
    run(source/'configure',*flags,cwd=build)
    run('make','-j'+jobs,cwd=build)
    run('make','install',cwd=build)
   else:
    run('cmake','-S',source,'-B',build,*flags)
    run('cmake','--build',build,'--parallel',jobs)
    run('cmake','--install',build)
   stamp.write_text(identity)
 ffmpeg=ROOT/'upstream/gpu-screen-recorder/subprojects/ffmpeg-9.0'
 fingerprint=hashlib.sha256(json.dumps(INPUTS,sort_keys=True).encode()).hexdigest()[:12]
 build=work/('ffmpeg-'+fingerprint+'-build');build.mkdir(exist_ok=True)
 flags=['--prefix='+str(prefix),'--disable-shared','--enable-static','--disable-doc','--disable-debug','--disable-ffplay','--disable-autodetect','--enable-gpl','--enable-version3','--enable-zlib','--pkg-config-flags=--static','--enable-libx264','--enable-libx265','--enable-libaom','--enable-libopus','--enable-libmp3lame','--enable-libfreetype','--enable-libharfbuzz','--enable-libfontconfig','--enable-libvidstab','--enable-libass','--enable-libpulse','--enable-ffnvcodec','--enable-nvenc','--enable-nvdec','--enable-cuda','--enable-vaapi','--enable-vulkan','--extra-cflags='+environment['CFLAGS']]
 stamp=build/'layer-configuration.json'
 if not stamp.exists() or json.loads(stamp.read_text())!=flags:
  run(ffmpeg/'configure',*flags,cwd=build)
  stamp.write_text(json.dumps(flags))
 if deps_changed:
  for name in ('ffmpeg','ffprobe','ffmpeg_g','ffprobe_g'):(build/name).unlink(missing_ok=True)
 run('make','-j'+jobs,'ffmpeg','ffprobe',cwd=build)
 for name in ('ffmpeg','ffprobe'):
  destination=Path('/app/bin')/name;shutil.copy2(build/name,destination)
  run('strip','--strip-unneeded',destination)
 # Fail the build before packaging if editor-critical capabilities are missing.
 for argument,required in [('-filters',{'ass','drawtext','vidstabdetect','vidstabtransform','loudnorm','overlay','geq','xfade','astats','ametadata'}),('-encoders',{'png','libx264','libx265','libaom-av1','h264_nvenc','hevc_nvenc','h264_vaapi','hevc_vaapi','av1_vaapi'}),('-decoders',{'png','mjpeg','webp'})]:
  listing=subprocess.check_output(['/app/bin/ffmpeg','-hide_banner',argument],env=environment,text=True,stderr=subprocess.DEVNULL)
  names={columns[1] for line in listing.splitlines() if len(columns:=line.split())>=2}
  missing=required-names
  if missing:raise RuntimeError('Editor FFmpeg is missing '+argument+': '+', '.join(sorted(missing)))
 licenses=Path('/app/share/licenses/io.github.miruas.Layer/renderer');licenses.mkdir(parents=True,exist_ok=True)
 for name,(_,_,directory) in INPUTS.items():
  source=work/directory
  if name=='x265':source=source.parent
  for path in source.iterdir():
   if path.is_file() and path.name.upper().startswith(('COPYING','LICENSE','NOTICE','COPYRIGHT','PATENTS')):
    target=licenses/name;target.mkdir(exist_ok=True);shutil.copy2(path,target/path.name)


if __name__=='__main__':main()
