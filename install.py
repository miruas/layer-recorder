#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Install user-local launchers/services; never copy host configuration into source."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def systemd_quote(value):
    value=str(value)
    if '\n' in value:raise ValueError('Installation paths cannot contain newlines.')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def write(path,text,executable=False):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    if executable:path.chmod(0o755)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall',action='store_true');parser.add_argument('--enable',action='store_true',help='Enable capture service at graphical login')
    parser.add_argument('--start',action='store_true',help='Start the service now; never restart an existing capture')
    parser.add_argument('--overlay',action='store_true',help='Install the locally built GSR overlay override; do not restart GSR')
    parser.add_argument('--gsr-service',default='gsr-ui.service');parser.add_argument('--library');parser.add_argument('--profile',choices=['auto','nvidia','amd','intel'],default='auto')
    args=parser.parse_args();home=Path.home();config=Path(os.environ.get('XDG_CONFIG_HOME',home/'.config'));data=Path(os.environ.get('XDG_DATA_HOME',home/'.local/share'));bin_dir=home/'.local/bin'
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',args.gsr_service):parser.error('Use a simple user service name.')
    unit=config/'systemd/user/katman.service';dropin=config/('systemd/user/'+args.gsr_service+'.d/layer.conf')
    files=[bin_dir/'layer',bin_dir/'katman',data/'applications/katman.desktop',data/'mime/packages/katman.xml',unit,config/'autostart/layer-recorder.desktop']
    def systemctl(*parts):
        try:return subprocess.run(['systemctl','--user',*parts],capture_output=True,text=True,timeout=15)
        except (OSError,subprocess.SubprocessError):return subprocess.CompletedProcess(['systemctl'],127,'','User service manager unavailable')
    def owned(file):
        return str(ROOT) in file.read_text() or (file==files[3] and '<comment>Layer recording project</comment>' in file.read_text())
    if args.uninstall:
        result=systemctl('is-active','katman.service')
        if result.returncode==0:parser.error('Stop/save active capture with layer shutdown before uninstalling.')
        for file in files+[dropin]:
            if file.exists() and not owned(file):parser.error('Refusing to remove an unrelated file: '+str(file))
        systemctl('disable','katman.service')
        for file in files+[dropin]:
            file.unlink(missing_ok=True)
        systemctl('daemon-reload');print('Layer installation links removed. Projects and settings are preserved.');return
    from katman.configuration import profile,validate_config
    from katman.common import atomic_json
    # Refuse unrelated pre-existing integrations rather than overwrite them.
    for file in files:
        if file.exists() and not owned(file):parser.error('An existing installation owns '+str(file)+'. Use a separate account or remove its links before installing this checkout.')
    if args.overlay:
        binary=ROOT/'overlay/bin/gsr-ui'
        if not binary.is_file():parser.error('Build the overlay with tools/build-native.py gsr-ui first.')
        if not (config/('systemd/user/'+args.gsr_service)).exists():parser.error('The specified user GSR service does not exist. Install/start GSR separately first.')
        if dropin.exists() and not owned(dropin):parser.error('An unrelated GSR override already exists.')
    launch=str(ROOT/'run.sh');interpreter=sys.executable
    launcher='#!/bin/sh\n# Layer launcher\nexport LAYER_PYTHON='+shlex.quote(interpreter)+'\nexec '+shlex.quote(launch)+' "$@"\n'
    write(files[0],launcher,True);write(files[1],launcher,True)
    desktop_exec='"'+launch.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    write(files[2],f'[Desktop Entry]\nType=Application\nName=Layer\nComment=Independent-source recording and editing\nExec={desktop_exec} ui %f\nIcon={ROOT}/katman/assets/katman.svg\nTerminal=false\nCategories=AudioVideo;Recorder;Video;\nMimeType=application/x-katman-project;\nStartupWMClass=layer-recorder\n')
    write(files[3],'<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info"><mime-type type="application/x-katman-project"><comment>Layer recording project</comment><glob pattern="*.katman"/></mime-type></mime-info>\n')
    write(unit,f'''[Unit]
Description=Layer multi-source recording service
After=graphical-session.target
PartOf=graphical-session.target
Requisite=graphical-session.target

[Service]
Type=simple
Environment={systemd_quote('PYTHONPATH='+str(ROOT))}
Environment={systemd_quote('PATH='+str(bin_dir)+':/usr/local/bin:/usr/bin:/bin')}
ExecStart={systemd_quote(interpreter)} -m katman.daemon
WorkingDirectory={systemd_quote(ROOT)}
Restart=on-failure
RestartSec=3
TimeoutStopSec=120

[Install]
WantedBy=graphical-session.target
''')
    settings=config/'katman/settings.json'
    if not settings.exists():
        values=profile(args.profile)
        if args.library:values['library']=args.library
        atomic_json(settings,validate_config(values))
    if args.overlay:
        binary=ROOT/'overlay/bin/gsr-ui'
        if not binary.is_file():parser.error('Build the overlay with tools/build-native.py gsr-ui first.')
        if not (config/('systemd/user/'+args.gsr_service)).exists():parser.error('The specified user GSR service does not exist. Install/start GSR separately first.')
        write(dropin,f'[Unit]\nWants=katman.service\nAfter=katman.service\n\n[Service]\nEnvironment=LANG=C.UTF-8\nEnvironment=LC_ALL=C.UTF-8\nEnvironment={systemd_quote("PATH="+str(bin_dir)+":/usr/local/bin:/usr/bin:/bin")}\nExecStart=\nExecStart={systemd_quote(binary)} launch-daemon\n')
    systemctl('daemon-reload')
    if args.enable:
        systemctl('enable','katman.service')
        write(files[5],f'[Desktop Entry]\nType=Application\nName=Layer recording service\nComment=Start independent-source replay at desktop login\nExec={desktop_exec} autostart\nTerminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n')
    if args.start:
        result=systemctl('start','katman.service')
        if result.returncode:
            env=os.environ.copy();env['PYTHONPATH']=str(ROOT)
            subprocess.run([interpreter,'-m','katman','autostart'],env=env,cwd=ROOT,check=True,timeout=30)
    for command,path in [('update-desktop-database',data/'applications'),('update-mime-database',data/'mime')]:
        if __import__('shutil').which(command):subprocess.run([command,str(path)],capture_output=True)
    print('Layer installed. Add ~/.local/bin to PATH. Existing settings and desktop hotkeys were preserved.')
    if args.overlay:print('Overlay override installed. Restart your GSR service only after saving/stopping its capture.')

if __name__=='__main__':main()
