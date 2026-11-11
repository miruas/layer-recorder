# SPDX-License-Identifier: GPL-3.0-only
"""Selective GSR preference integration with private backups and no title data."""
import json,os,shutil,subprocess,time
from pathlib import Path
from .common import APP,CONFIG,atomic_json
from .shortcuts import parse

HOTKEYS={'overlay':('main','show_hide_hotkey'),'record':('record','start_stop_hotkey'),'replay':('replay','start_stop_hotkey'),'save-1':('replay','save_1_min_hotkey'),'save-5':('replay','save_hotkey')}
LABELS={'overlay':'Show / hide GSR overlay','record':'GSR start / stop recording','replay':'GSR replay on / off','save-1':'GSR one-minute save','save-5':'GSR full-buffer save'}
KEYS=json.loads((Path(__file__).parent/'assets/gsr-keys.json').read_text())


def cli():
    local=APP/'overlay/bin/gsr-ui-cli'
    return str(local) if local.is_file() else shutil.which('gsr-ui-cli')


def config_path():
    from .gsr_preferences import config_path as canonical
    return canonical()
def read():
    file=config_path()
    if not file.exists():return None
    return json.loads(file.read_text())


def decode(hotkey):
    key=next((k for k,v in KEYS.items() if v==hotkey.get('key')),None)
    if not key:return ''
    mask=hotkey.get('modifiers',0);mods=[label for bits,label in [(12,'Ctrl'),(48,'Alt'),(3,'Shift'),(192,'Meta')] if mask&bits]
    return '+'.join(mods+[key])


def encode(sequence):
    pair=parse(sequence)
    if not pair:return {'key':0,'modifiers':0}
    mask,key=pair
    if key not in KEYS:raise ValueError('GSR supports letters, digits, function keys and standard navigation keys here.')
    modifiers=sum(target for source,target in [(4,4),(8,16),(1,1),(64,64)] if mask&source)
    return {'key':KEYS[key],'modifiers':modifiers}


def hotkeys(config):return {action:decode(config.get(section,{}).get(key,{})) for action,(section,key) in HOTKEYS.items()}


def update(layer,bindings=None):
    data=read()
    if data is None:raise RuntimeError('Open GPU Screen Recorder once to create its preferences before connecting Layer.')
    if layer['gsr_sync']:
        for section in ('record','replay'):
            options=data.setdefault(section,{}).setdefault('record_options',{});options['codec']=layer['capture_codec'];options['show_notifications']=False
        data['replay']['time']=layer['buffer']
    if bindings is not None:
        encoded={action:data.get(section,{}).get(key,{'key':0,'modifiers':0}) for action,(section,key) in HOTKEYS.items()}
        encoded.update({action:encode(value) for action,value in bindings.items()})
        active=[(x['key'],x['modifiers']) for x in encoded.values() if x['key']]
        if len(active)!=len(set(active)):raise ValueError('Give each GSR action a different shortcut.')
        for action,value in encoded.items():
            section,key=HOTKEYS[action];data.setdefault(section,{})[key]=value
    folder=Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))/'layer/backups';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    backup=folder/('gsr-config-'+str(time.time_ns())+'.json');shutil.copy2(config_path(),backup);backup.chmod(0o600)
    atomic_json(config_path(),data)
    if cli():subprocess.run([cli(),'reload-config'],capture_output=True,timeout=5)
    return backup
