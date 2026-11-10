# SPDX-License-Identifier: GPL-3.0-only
"""Validated, portable configuration. No machine-specific defaults."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    'library': '~/Videos/Layer', 'fps': 60, 'bitrate': 18000, 'buffer': 300,
    'auto_replay': False, 'gpu_vendor': 'auto', 'capture_backend': 'auto',
    'capture_codec': 'h264', 'render_device': 'auto', 'wf_no_dmabuf': False,
    'export_codec': 'libx264', 'export_quality': 20,
    'gsr_binary': 'auto', 'wf_recorder_binary': '',
    'speech_binary': '', 'speech_model': '',
}
CODECS = ('libx264','libx265','libaom-av1','h264_nvenc','hevc_nvenc','av1_nvenc','h264_vaapi','hevc_vaapi','av1_vaapi')


def validate_config(values):
    if not isinstance(values,dict):raise ValueError('Settings must be a JSON object.')
    unknown=set(values)-set(DEFAULTS)
    if unknown:raise ValueError('Unknown setting: '+', '.join(sorted(unknown)))
    result=copy.deepcopy(DEFAULTS);result.update(values)
    for key,low,high in [('fps',1,240),('bitrate',1000,200000),('buffer',300,300),('export_quality',0,51)]:
        if type(result[key]) is not int or not low<=result[key]<=high:raise ValueError(f'{key} must be an integer from {low} to {high}.')
    for key in ('auto_replay','wf_no_dmabuf'):
        if type(result[key]) is not bool:raise ValueError(key+' must be true or false.')
    for key,choices in [('gpu_vendor',('auto','nvidia','amd','intel')),('capture_backend',('auto','gsr','wf-recorder')),('capture_codec',('h264','hevc','av1')),('export_codec',CODECS)]:
        if result[key] not in choices:raise ValueError(key+' must be one of: '+', '.join(choices))
    for key in ('library','render_device','gsr_binary','wf_recorder_binary','speech_binary','speech_model'):
        if not isinstance(result[key],str) or '\x00' in result[key] or '\n' in result[key]:raise ValueError(key+' must be a path or program name without control characters.')
    if not result['library'].strip() or not result['gsr_binary'].strip():raise ValueError('Library and GSR program cannot be empty.')
    device=result['render_device']
    if device!='auto' and not __import__('re').fullmatch(r'/dev/dri/renderD\d+',device):raise ValueError('render_device must be auto or /dev/dri/renderD<number>.')
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
