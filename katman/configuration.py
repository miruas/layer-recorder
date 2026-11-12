# SPDX-License-Identifier: GPL-3.0-only
"""Validated, portable configuration. No machine-specific defaults."""
import copy
import json
import os
import math
import re
from pathlib import Path

DEFAULTS = {
    'library': '~/Videos/Layer', 'fps': 60, 'bitrate': 18000, 'buffer': 300,
    'auto_replay': False, 'replay_short':60, 'replay_long':300,
    'shortcuts_enabled':True, 'shortcuts':{'ui':'Meta+Alt+Z','save-1':'Meta+Alt+F7','save-5':'Meta+Alt+F8','record':'Meta+Alt+F9','replay':'Meta+Alt+F10'}, 'gsr_sync':False, 'gsr_preferences':True, 'gsr_source':'gsr', 'gsr_config':'', 'gpu_vendor': 'auto', 'capture_backend': 'auto',
    'capture_advanced': {}, 'capture_resolution': 'native', 'capture_targets': [], 'capture_codec': 'h264', 'capture_quality': 'custom', 'render_device': 'auto', 'wf_no_dmabuf': False,
    'preview_resolution': 'native', 'preview_fps': 'source',
    'export_codec': 'libx264', 'export_quality': 20,
    'gsr_binary': 'auto', 'wf_recorder_binary': '',
    'speech_binary': '', 'speech_model': '',
}
CODECS = ('libx264','libx265','libaom-av1','h264_nvenc','hevc_nvenc','av1_nvenc','h264_vaapi','hevc_vaapi','av1_vaapi')
CAPTURE_CODECS = ('auto','h264','hevc','hevc_10bit','hevc_hdr','av1','av1_10bit','av1_hdr','vp8','vp9','h264_vulkan','hevc_vulkan','hevc_hdr_vulkan','hevc_10bit_vulkan','av1_vulkan','av1_hdr_vulkan','av1_10bit_vulkan')
CAPTURE_QUALITIES = ('custom','medium','high','very_high','ultra')


RESOLUTION_PRESETS = {'native': None, '720p': (1280, 720), '1080p': (1920, 1080),
                      '1440p': (2560, 1440), '4k': (3840, 2160)}


def validate_capture_resolution(value):
    """A native/preset name or an even, bounded width/height object."""
    if isinstance(value, str) and value in RESOLUTION_PRESETS:
        return copy.deepcopy(value)
    if isinstance(value, dict) and set(value) == {'width', 'height'}:
        if all(type(value[k]) is int and 32 <= value[k] <= 32768 and value[k] % 2 == 0
               for k in ('width', 'height')):
            return dict(value)
    raise ValueError('capture_resolution must be native, 720p, 1080p, 1440p, 4k, or an object with even width and height from 32 to 32768.')


def validate_preview_resolution(value):
    if isinstance(value, str) and value in ('native', '1080p', '1440p', '4k'):
        return value
    if isinstance(value, dict):
        try:
            return validate_capture_resolution(value)
        except ValueError:
            pass
    raise ValueError('preview_resolution must be native, 1080p, 1440p, 4k, or an object with even width and height from 32 to 32768.')


# Only codec-private options are permitted: muxer/segment and timeline controls stay managed.
VIDEO_OPTIONS = {'preset', 'profile', 'level', 'rc_mode', 'qp', 'cq', 'b_ref_mode',
                 'spatial_aq', 'temporal_aq', 'aq-strength', 'look_ahead', 'coder'}
AUDIO_OPTIONS = {'aac_coder', 'aac_pce', 'compression_level', 'application',
                 'frame_duration', 'vbr', 'cutoff'}
ADVANCED_ENUMS = {'bitrate_mode': ('auto','qp','vbr','cbr'),
    'framerate_mode': ('cfr','vfr','content'), 'pixel_format': ('yuv420','yuv444'),
    'color_range': ('limited','full'), 'tune': ('performance','quality'),
    'encoder': ('gpu','cpu'), 'audio_codec': ('opus','aac','mp3')}
ADVANCED_BOOLS = ('cursor','fallback_cpu_encoding','low_power','restore_portal_session',
                  'overclock','verbose','gl_debug','exclude_metadata')


def validate_capture_advanced(value):
    if not isinstance(value, dict):
        raise ValueError('capture_advanced must be an object.')
    allowed = set(ADVANCED_ENUMS) | set(ADVANCED_BOOLS) | {
        'keyint','portal_session_token_filepath','region','audio_bitrate','plugins',
        'video_options','audio_options'}
    if set(value)-allowed:
        raise ValueError('Unknown capture_advanced option: '+', '.join(sorted(set(value)-allowed)))
    result = copy.deepcopy(value)
    for key, choices in ADVANCED_ENUMS.items():
        if key in value and value[key] not in choices:
            raise ValueError(key+' must be one of: '+', '.join(choices))
    for key in ADVANCED_BOOLS:
        if key in value and type(value[key]) is not bool:
            raise ValueError(key+' must be true or false.')
    if 'keyint' in value and (type(value['keyint']) not in (int,float) or
            not math.isfinite(value['keyint']) or not 0 <= value['keyint'] <= 2):
        raise ValueError('keyint must be from 0 to 2 seconds to preserve bounded segments.')
    if 'audio_bitrate' in value and (type(value['audio_bitrate']) is not int or not 0 <= value['audio_bitrate'] <= 50000):
        raise ValueError('audio_bitrate must be an integer from 0 to 50000.')
    if 'region' in value:
        region=value['region']
        if (not isinstance(region,dict) or set(region)!={'width','height','x','y'} or
            any(type(v) is not int for v in region.values()) or
            any(not 0 <= region[k] <= 32768 for k in ('width','height')) or
            any(not -131072 <= region[k] <= 131072 for k in ('x','y'))):
            raise ValueError('region needs integer width/height (0..32768) and x/y (-131072..131072).')
    def path_ok(path):
        return isinstance(path,str) and 0 < len(path) <= 4096 and not any(ord(c)<32 or c in '{};' for c in path)
    if 'portal_session_token_filepath' in value and not path_ok(value['portal_session_token_filepath']):
        raise ValueError('portal_session_token_filepath must be a nonempty path without control characters or placeholders.')
    if 'plugins' in value:
        plugins=value['plugins']
        if (not isinstance(plugins,list) or len(plugins)>16 or
            any(not path_ok(p) or not Path(p).is_absolute() or not p.endswith('.so') for p in plugins)):
            raise ValueError('plugins must contain at most 16 absolute .so paths.')
    for key, options in (('video_options',VIDEO_OPTIONS),('audio_options',AUDIO_OPTIONS)):
        if key not in value:continue
        items=value[key]
        if not isinstance(items,dict) or set(items)-options:
            raise ValueError(key+' contains unsupported codec options.')
        for name, item in items.items():
            if type(item) not in (str,int,float,bool) or not re.fullmatch(r'[A-Za-z0-9_.+:/-]{1,128}',str(item)):
                raise ValueError(key+'.'+name+' must be a simple scalar without option separators.')
            if isinstance(item,float) and not math.isfinite(item):raise ValueError('Codec options must be finite.')
    return result


def codec_options(value):
    return ';'.join(f'{key}={str(item).lower() if type(item) is bool else item}'
                    for key,item in sorted(value.items()))


def capture_target_base(target):
    base=target.split(';',1)[0]
    for prefix in ('monitor:','window:','v4l2:'):
        if base.startswith(prefix):return base[len(prefix):]
    return base


def static_capture_target(target):
    base=capture_target_base(target)
    return (base in ('portal','focused','region','screen','screen-direct') or
            base.isdecimal() or base.startswith(('0x','/dev/video')) or
            bool(re.fullmatch(r'\d+x\d+\+[+-]?\d+\+[+-]?\d+',base)))


def validate_capture_target(target):
    if not isinstance(target,str) or not target or len(target)>1024 or any(ord(c)<32 or c in '|{}' for c in target):
        raise ValueError('capture_targets needs individual source names without control characters, pipes or placeholders.')
    parts=target.split(';')
    if not parts[0] or len(parts[0])>63:raise ValueError('GSR source identifiers must contain 1..63 characters.')
    seen=set()
    for part in parts[1:]:
        key,sep,value=part.partition('=')
        if not sep or key in seen:raise ValueError('Capture source modifiers must be unique key=value pairs.')
        seen.add(key)
        valid=False
        if key in ('x','y','width','height'):
            valid=bool(re.fullmatch(r'[+-]?\d{1,9}%?',value))
            if valid and key in ('width','height'):valid=int(value.rstrip('%'))>=0
        elif key in ('halign','valign'):valid=value in ('start','center','end')
        elif key in ('hflip','vflip'):valid=value in ('true','false')
        elif key=='pixfmt':valid=value in ('auto','yuyv','mjpeg')
        elif key in ('camera_fps','camera_width','camera_height'):valid=bool(re.fullmatch(r'\d{1,9}',value))
        if not valid:raise ValueError('Unsupported capture source modifier: '+part)
    return target


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
    result['capture_advanced']=validate_capture_advanced(result['capture_advanced'])
    advanced=result['capture_advanced']
    if advanced.get('encoder')=='cpu' and result['capture_codec'] not in ('auto','h264'):
        raise ValueError('GSR CPU encoding supports only auto/h264.')
    mode=advanced.get('bitrate_mode')
    if mode=='cbr' and result['capture_quality']!='custom':raise ValueError('CBR needs capture_quality custom and bitrate.')
    if mode in ('auto','qp','vbr') and result['capture_quality']=='custom':raise ValueError('QP/VBR/auto need a named capture_quality preset.')
    result['capture_resolution']=validate_capture_resolution(result['capture_resolution'])
    result['preview_resolution']=validate_preview_resolution(result['preview_resolution'])
    if result['preview_fps'] != 'source' and not (type(result['preview_fps']) is int and result['preview_fps'] in (60,120)):
        raise ValueError('preview_fps must be source, 60 or 120.')
    targets=result['capture_targets']
    if not isinstance(targets,list) or len(targets)>16:raise ValueError('capture_targets must contain at most 16 individual GSR sources.')
    for target in targets:validate_capture_target(target)
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
