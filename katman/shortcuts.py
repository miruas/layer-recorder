# SPDX-License-Identifier: GPL-3.0-only
"""Owned Hyprland bindings; foreign shortcuts are never removed or replaced."""
import json,re,shlex,subprocess,threading
from pathlib import Path

ACTIONS={'ui':'Open Layer','record':'Start / stop recording','replay':'Enable / disable replay','save-1':'Save short replay','save-5':'Save long replay'}
MODS={'CTRL':4,'ALT':8,'SHIFT':1,'SUPER':64}
ALIASES={'CONTROL':'CTRL','META':'SUPER','WIN':'SUPER'}


def parse(sequence):
    if not sequence:return None
    parts=[p.strip().upper() for p in sequence.split('+')];key=parts.pop();mods=[]
    for part in parts:
        part=ALIASES.get(part,part)
        if part not in MODS or part in mods:raise ValueError('Use one key with Control, Alt, Shift or Super modifiers.')
        mods.append(part)
    if not re.fullmatch(r'[A-Z0-9_]+',key) or len(key)>24:raise ValueError('Use a letter, number, function key or named keyboard key.')
    return sum(MODS[m] for m in mods),key


def command(action):
    from .common import APP
    return shlex.quote(str(APP/'run.sh'))+' '+action


def inventory():
    p=subprocess.run(['hyprctl','binds','-j'],capture_output=True,text=True,timeout=3)
    if p.returncode:raise RuntimeError('Hyprland shortcuts are unavailable in this session.')
    return json.loads(p.stdout)


def bound_command(binding):
    if binding.get('dispatcher')=='exec':return binding.get('arg')
    if binding.get('dispatcher')=='__lua' and binding.get('description','').startswith('Layer shortcut: '):return binding['description'][16:]
    return None


def owned(binding):return bound_command(binding) in {value for action in ACTIONS for value in (command(action),'layer '+action,'/usr/bin/layer '+action)}
def signature(binding):return int(binding.get('modmask',0)),str(binding.get('key','')).upper()
def chord(pair):return ' '.join(name for name,value in MODS.items() if pair[0]&value)+', '+pair[1]


def check_conflicts(config,bindings=None):
    if not config['shortcuts_enabled']:return
    bindings=inventory() if bindings is None else bindings
    for action,sequence in config['shortcuts'].items():
        pair=parse(sequence)
        if pair and any(signature(b)==pair and not owned(b) for b in bindings):raise ValueError(ACTIONS[action]+': '+sequence+' is already used by another application or compositor binding.')


def sync(config):
    bindings=inventory();check_conflicts(config,bindings)
    desired={parse(value):action for action,value in config['shortcuts'].items() if value} if config['shortcuts_enabled'] else {}
    def keyword(name,value):
        p=subprocess.run(['hyprctl','keyword',name,value],capture_output=True,text=True,timeout=3)
        if "keyword can't work with non-legacy parsers" in p.stdout:
            parts=value.split(', ',3);keys=' + '.join(parts[0].split()+[parts[1]])
            quote=lambda text:json.dumps(text,ensure_ascii=False)
            script=('hl.unbind('+quote(keys)+')') if name=='unbind' else ('hl.bind('+quote(keys)+', hl.dsp.exec_cmd('+quote(parts[3])+'), {description='+quote('Layer shortcut: '+parts[3])+', allow_input_capture=true})')
            p=subprocess.run(['hyprctl','eval',script],capture_output=True,text=True,timeout=3)
            if p.stdout.strip()!='ok':raise RuntimeError('Could not apply a Layer shortcut: '+p.stderr.strip()+p.stdout.strip())
        if p.returncode:raise RuntimeError('Could not apply a Layer shortcut: '+p.stderr.strip()+p.stdout.strip())
    mine={signature(b) for b in bindings if owned(b)}
    for pair in mine:
        entries=[b for b in bindings if signature(b)==pair]
        if any(not owned(b) for b in entries):continue
        action=desired.get(pair)
        if len(entries)==1 and action and bound_command(entries[0])==command(action):continue
        keyword('unbind',chord(pair));bindings=[b for b in bindings if signature(b)!=pair]
    for pair,action in desired.items():
        if not any(signature(b)==pair and owned(b) and bound_command(b)==command(action) for b in bindings):keyword('bind',chord(pair)+', exec, '+command(action))


class Watcher:
    def __init__(self):self.stop=threading.Event();self.error='';self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        from .common import settings
        while not self.stop.is_set():
            try:sync(settings());self.error=''
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:self.error=str(e)
            self.stop.wait(5)
