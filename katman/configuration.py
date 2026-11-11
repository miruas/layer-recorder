# SPDX-License-Identifier: GPL-3.0-only
"""Validated, portable configuration. No machine-specific defaults."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    'library': '~/Videos/Layer', 'fps': 60, 'bitrate': 18000, 'buffer': 300,
    'auto_replay': False, 'replay_short':60, 'replay_long':300,
    'shortcuts_enabled':True, 'shortcuts':{'ui':'Meta+Alt+Z','save-1':'Meta+Alt+F7','save-5':'Meta+Alt+F8','record':'Meta+Alt+F9','replay':'Meta+Alt+F10'}, 'gsr_sync':False, 'gsr_preferences':True, 'gsr_source':'gsr', 'gsr_config':'', 'gpu_vendor': 'auto', 'capture_backend': 'auto',
    'capture_targets': [], 'capture_codec': 'h264', 'capture_quality': 'custom', 'render_device': 'auto', 'wf_no_dmabuf': False,
    'export_codec': 'libx264', 'export_quality': 20,
    'gsr_binary': 'auto', 'wf_recorder_binary': '',
    'speech_binary': '', 'speech_model': '',
}
CODECS = ('libx264','libx265','libaom-av1','h264_nvenc','hevc_nvenc','av1_nvenc','h264_vaapi','hevc_vaapi','av1_vaapi')
CAPTURE_CODECS = ('auto','h264','hevc','hevc_10bit','hevc_hdr','av1','av1_10bit','av1_hdr')
CAPTURE_QUALITIES = ('custom','medium','high','very_high','ultra')


def validate_config(values):
    if not isinstance(values,dict):raise ValueError('Settings must be a JSON object.')
    unknown=set(values)-set(DEFAULTS)
    if unknown:raise ValueError('Unknown setting: '+', '.join(sorted(unknown)))
    result=copy.deepcopy(DEFAULTS);result.update(values)
    for key,low,high in [('fps',1,240),('bitrate',1000,200000),('buffer',2,86400),('replay_short',1,86400),('replay_long',1,86400),('export_quality',0,51)]:
        if type(result[key]) is not int or not low<=result[key]<=high:raise ValueError(f'{key} must be an integer from {low} to {high}.')
    for key in ('auto_replay','wf_no_dmabuf','shortcuts_enabled','gsr_sync','gsr_preferences'):
        if type(result[key]) is not bool:raise ValueError(key+' must be true or false.')
    for key,choices in [('gpu_vendor',('auto','nvidia','amd','intel')),('capture_backend',('auto','gsr')),('capture_codec',CAPTURE_CODECS),('capture_quality',CAPTURE_QUALITIES),('gsr_source',('gsr','layer','auto','off')),('export_codec',CODECS)]:
        if result[key] not in choices:raise ValueError(key+' must be one of: '+', '.join(choices))
    targets=result['capture_targets']
    if not isinstance(targets,list) or len(targets)>16 or any(not isinstance(x,str) or not x or len(x)>256 or any(c in x for c in ('\n','\r','\0','|')) for x in targets):raise ValueError('capture_targets must contain at most 16 individual GSR sources.')
    if len(set(targets))!=len(targets):raise ValueError('Capture targets must be unique.')
    if result['wf_no_dmabuf'] or result['wf_recorder_binary']:raise ValueError('The wf-recorder backend was removed. Clear its legacy settings and select GSR capture_targets.')
    for key in ('library','render_device','gsr_binary','gsr_config','wf_recorder_binary','speech_binary','speech_model'):
        if not isinstance(result[key],str) or '\x00' in result[key] or '\n' in result[key]:raise ValueError(key+' must be a path or program name without control characters.')
    if not result['library'].strip() or not result['gsr_binary'].strip():raise ValueError('Library and GSR program cannot be empty.')
    device=result['render_device']
    if device!='auto' and not __import__('re').fullmatch(r'/dev/dri/renderD\d+',device):raise ValueError('render_device must be auto or /dev/dri/renderD<number>.')
    if not result['replay_short']<=result['replay_long']<=result['buffer']:raise ValueError('Replay durations must satisfy short ≤ long ≤ buffer.')
    from .shortcuts import ACTIONS,parse
    if not isinstance(result['shortcuts'],dict) or set(result['shortcuts'])!=set(ACTIONS):raise ValueError('Configure each of the five Layer shortcut actions.')
    seen=set()
    for sequence in result['shortcuts'].values():
        if not isinstance(sequence,str):raise ValueError('Shortcut values must be text.')
        pair=parse(sequence)
        if pair and pair in seen:raise ValueError('Each Layer action needs a different shortcut.')
        if pair:seen.add(pair)
    return result


def load_settings(path):
    file=Path(path)
    try:values=json.loads(file.read_text()) if file.exists() else {}
    except (OSError,json.JSONDecodeError) as e:raise ValueError('Could not read settings: '+str(e)) from e
    return validate_config(values)


def profile(name):
    if name not in ('auto','nvidia','amd','intel'):raise ValueError('Unknown GPU profile.')
    result=copy.deepcopy(DEFAULTS);result['gpu_vendor']=name
    if name=='nvidia':result['export_codec']='h264_nvenc'
    if name in ('amd','intel'):result['export_codec']='h264_vaapi'
    return result
