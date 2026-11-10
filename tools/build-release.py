#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Assemble an Arch/CachyOS binary package and complete corresponding sources."""
import argparse,hashlib,json,os,platform,re,shlex,shutil,subprocess,tarfile,time,tomllib
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent


def run(args,**kwargs):return subprocess.run([str(x) for x in args],check=True,**kwargs)
def copy_tree(source,dest):
    for raw in subprocess.check_output(['git','ls-files','-z'],cwd=source).decode().split('\0'):
        if not raw:continue
        file=source/raw;target=dest/raw
        if file.is_dir():copy_tree(file,target)
        elif file.is_file():target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--overlay-prefix',type=Path,required=True,help='DESTDIR containing a build configured for /usr/lib/layer-recorder/overlay');parser.add_argument('--speech-binary',type=Path);args=parser.parse_args()
    if platform.machine()!='x86_64':parser.error('This recipe currently targets Arch/CachyOS x86_64.')
    inside=os.environ.get('LAYER_RELEASE_CONTAINER')=='1'
    for tool in (('makepkg','strip','readelf') if inside else ('docker','strip','readelf')):
        if not shutil.which(tool):parser.error('Missing release tool: '+tool)
    version=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version'];work=ROOT/'build/release';payload=work/'package/payload';out=ROOT/'dist';out.mkdir(exist_ok=True)
    if payload.exists():shutil.rmtree(payload)
    app=payload/'usr/lib/layer-recorder';app.mkdir(parents=True)
    shutil.copytree(ROOT/'katman',app/'katman',ignore=shutil.ignore_patterns('__pycache__','*.pyc'));shutil.copy2(ROOT/'run.sh',app/'run.sh')
    for binary in ('gpu-screen-recorder','gsr-cli','katman-wf-recorder'):
        file=ROOT/'bin'/binary
        if not file.is_file():parser.error('Build native component first: '+binary)
        (app/'bin').mkdir(exist_ok=True);shutil.copy2(file,app/'bin'/binary)
    overlay=args.overlay_prefix/'usr/lib/layer-recorder/overlay'
    if not (overlay/'bin/gsr-ui').is_file():parser.error('The overlay build must use prefix /usr/lib/layer-recorder/overlay.')
    shutil.copytree(overlay,app/'overlay')
    if args.speech_binary:
        (app/'speech').mkdir();shutil.copy2(args.speech_binary,app/'speech/whisper-cli')
    (app/'tools').mkdir();shutil.copy2(ROOT/'tools/download-model.py',app/'tools/download-model.py')
    license_dir=payload/'usr/share/licenses/layer-recorder';license_dir.mkdir(parents=True)
    for name in ('LICENSE','NOTICE.md','UPSTREAM.md'):shutil.copy2(ROOT/name,license_dir/name)
    shutil.copytree(ROOT/'licenses',license_dir/'components')
    docs=payload/'usr/share/doc/layer-recorder';shutil.copytree(ROOT/'docs',docs);shutil.copytree(ROOT/'examples',docs/'examples');shutil.copy2(ROOT/'README.md',docs/'README.md')
    def write(name,text,mode=0o644):
        file=payload/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text);file.chmod(mode)
    write('usr/bin/layer','#!/bin/sh\nexport PYTHONPATH=/usr/lib/layer-recorder${PYTHONPATH:+:$PYTHONPATH}\nexec /usr/bin/python3 -m katman "$@"\n',0o755)
    write('usr/bin/layer-download-model','#!/bin/sh\nexport PYTHONPATH=/usr/lib/layer-recorder${PYTHONPATH:+:$PYTHONPATH}\nexec /usr/bin/python3 /usr/lib/layer-recorder/tools/download-model.py "$@"\n',0o755)
    write('usr/share/applications/katman.desktop','[Desktop Entry]\nType=Application\nName=Layer\nComment=Independent recording and editing\nExec=layer ui %f\nIcon=/usr/lib/layer-recorder/katman/assets/katman.svg\nTerminal=false\nCategories=AudioVideo;Recorder;Video;\nMimeType=application/x-katman-project;\nStartupWMClass=katman\n')
    write('usr/share/mime/packages/katman.xml','<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info"><mime-type type="application/x-katman-project"><comment>Layer recording project</comment><glob pattern="*.katman"/></mime-type></mime-info>\n')
    write('usr/lib/systemd/user/katman.service','[Unit]\nDescription=Layer recording service\nAfter=graphical-session.target\nPartOf=graphical-session.target\nRequisite=graphical-session.target\n\n[Service]\nEnvironment=PYTHONPATH=/usr/lib/layer-recorder\nExecStart=/usr/bin/python3 -m katman.daemon\nWorkingDirectory=/usr/lib/layer-recorder\nRestart=on-failure\nRestartSec=3\nTimeoutStopSec=120\n\n[Install]\nWantedBy=graphical-session.target\n')
    # Never install host preferences or start captures as a package side effect.
    dependencies=['python>=3.12','pyside6','python-numpy','python-opencv','ffmpeg','gpu-screen-recorder','pipewire','libpulse','fontconfig','noto-fonts','libnotify','libglvnd','libva','libdrm','libx11','libxrandr','libxfixes','libxdamage','libxcomposite','libxi','libxcursor','dbus','pango','libdecor']
    sonames=set()
    for file in app.rglob('*'):
        if not file.is_file() or file.read_bytes()[:4]!=b'\x7fELF':continue
        run(['strip','--strip-unneeded',file])
        data=file.read_bytes()
        if str(Path.home()).encode() in data:parser.error('Personal build path embedded in '+str(file.relative_to(payload))+'. Rebuild with -ffile-prefix-map.')
        text=subprocess.check_output(['readelf','-d',str(file)],text=True)
        for name,abi in re.findall(r'\[(lib(?:avcodec|avformat|avutil|avfilter|avdevice|swresample|swscale)\.so)\.(\d+)\]',text):sonames.add(name+'='+abi+'-64')
    dependencies+=sorted(sonames)
    package_dir=payload.parent
    pkgbuild=f'''pkgname=layer-recorder
pkgver={version}
pkgrel=1
pkgdesc='GPLv3 GPU Screen Recorder fork with native multitrack editor'
arch=('x86_64')
url='https://github.com/miruas/layer-recorder'
license=('GPL-3.0-only')
depends=({' '.join(shlex.quote(x) for x in dependencies)})
options=('!debug' '!strip')
package() {{ cp -a "$startdir/payload/." "$pkgdir/"; }}
'''
    (package_dir/'PKGBUILD').write_text(pkgbuild)
    # Isolate makepkg's BUILDINFO: never publish the developer's installed package inventory or home paths.
    container='useradd -m -u 1000 builder; chown -R builder:builder /build /out; cd /build; runuser -u builder -- env PACKAGER="Layer contributors" PKGDEST=/out BUILDDIR=/build/build makepkg --force --nodeps --noconfirm --nosign'
    if inside:
        env=os.environ.copy();env.update(PACKAGER='Layer contributors',PKGDEST=str(out),BUILDDIR='/build/makepkg');run(['makepkg','--force','--nodeps','--noconfirm','--nosign'],cwd=package_dir,env=env)
    else:
        run(['docker','run','--rm','-v',str(package_dir)+':/build','-v',str(out)+':/out','archlinux:base-devel','bash','-lc',container])
    sources=work/'corresponding-sources';shutil.rmtree(sources,ignore_errors=True);sources.mkdir()
    copy_tree(ROOT,sources/'layer-recorder')
    for component in ('gsr-ui','wf-recorder','whisper'):
        source=ROOT/'build/native'/component/'source'
        if source.exists():copy_tree(source,sources/'external'/component)
        elif component=='whisper' and args.speech_binary:parser.error('Include the exact speech source checkout when distributing its executable.')
    archive=out/f'layer-recorder-{version}-corresponding-sources.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:tar.add(sources,arcname=f'layer-recorder-{version}-sources')
    package=out/f'layer-recorder-{version}-1-x86_64.pkg.tar.zst'
    checksums=out/f'layer-recorder-{version}-SHA256SUMS'
    checksums.write_text(''.join(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+file.name+'\n' for file in (package,archive)))
    print(json.dumps({'version':version,'artifacts':[str(p) for p in (package,archive,checksums)],'platform':'Arch/CachyOS x86_64','dependencies':dependencies},indent=2))

if __name__=='__main__':main()
