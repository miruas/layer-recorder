# SPDX-License-Identifier: GPL-3.0-only
"""Vendor-neutral command planning; hardware support is checked, never assumed."""
import functools
import json
import re
import shutil
import subprocess
from pathlib import Path

VENDORS={'0x10de':'nvidia','0x1002':'amd','0x8086':'intel'}


def devices(sys_root=Path('/sys/class/drm')):
    result=[]
    for node in sorted(Path(sys_root).glob('renderD*')):
        try:vendor=VENDORS.get((node/'device/vendor').read_text().strip().lower(),'unknown')
        except OSError:continue
        result.append({'vendor':vendor,'device':'/dev/dri/'+node.name})
    return result


def render_device(config,inventory=None):
    inventory=devices() if inventory is None else inventory
    vendor=config['gpu_vendor'];requested=config['render_device']
    candidates=[d for d in inventory if d['vendor'] in ('amd','intel') and (vendor=='auto' or d['vendor']==vendor)]
    if requested!='auto':
        candidate=next((d for d in candidates if d['device']==requested),None)
        if candidate:return candidate['device']
        raise RuntimeError('The configured VA-API render device does not match an available AMD/Intel GPU.')
    if not candidates:raise RuntimeError('No matching AMD/Intel render device found. Check the driver and render permissions, or choose the correct GPU profile.')
    return candidates[0]['device']


def gsr_program(config):
    if config['gsr_binary']!='auto':return config['gsr_binary']
    return str(Path(__file__).resolve().parent.parent/'bin/gpu-screen-recorder')


def gsr_info(program='gpu-screen-recorder'):
    p=subprocess.run([program,'--info'],capture_output=True,text=True,timeout=15)
    if p.returncode:raise RuntimeError('GPU Screen Recorder could not inspect capture capabilities: '+p.stderr[-1000:])
    section='';result={'vendor':'unknown','codecs':[]}
    for line in p.stdout.splitlines():
        if line.startswith('section='):section=line.split('=',1)[1]
        elif section=='system_info' and '|' in line:
            key,value=line.split('|',1)
            if key in ('gsr_version','fork_name','fork_version','upstream_revision','layer_audio_node_selector','layer_segment_muxer','display_server','supports_app_audio'):result[key]=value
        elif section=='gpu_info' and line.startswith('vendor|'):result['vendor']=line.split('|',1)[1]
        elif section=='video_codecs':result['codecs'].append(line.strip())
    return result


def capture_resolution_limit(display, value):
    """Return GSR bounds, clamped to known physical source size (never logical)."""
    from .configuration import RESOLUTION_PRESETS, validate_capture_resolution
    value = validate_capture_resolution(value)
    bounds = RESOLUTION_PRESETS[value] if isinstance(value, str) else (value['width'], value['height'])
    if bounds is None:
        return None
    width = display.get('capture_width', display.get('width', 0))
    height = display.get('capture_height', display.get('height', 0))
    if type(width) is int and type(height) is int and width > 1 and height > 1:
        scale = min(1.0, bounds[0] / width, bounds[1] / height)
        return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    # Portal geometry is only known once GSR opens the source. Its bounds
    # preserve aspect ratio; the recorder applies the no-upscale constraint.
    return bounds


def capture_command(display,config,direct,info,helper=None,inventory=None):
    vendor=config['gpu_vendor'];codec=config.get('capture_codec','h264');target=display['name']
    if vendor!='auto' and vendor!=info.get('vendor'):raise RuntimeError('GSR captures with the display GPU. The requested GPU profile does not match its advertised GPU.')
    if target not in direct:raise RuntimeError('This output is unavailable to GSR. Choose an explicit GSR portal source; automatic wf-recorder fallback was removed.')
    if codec!='auto' and codec not in info.get('codecs',[]) and config.get('capture_advanced',{}).get('encoder')!='cpu' and not config.get('capture_advanced',{}).get('fallback_cpu_encoding',False):raise RuntimeError('The capture GPU does not advertise '+codec+' encoding.')
    from .configuration import validate_capture_advanced, codec_options, capture_target_base
    advanced=validate_capture_advanced(config.get('capture_advanced',{}))
    encoder=advanced.get('encoder','gpu')
    if encoder=='cpu' and codec not in ('auto','h264'):
        raise ValueError('GSR CPU encoding supports only auto/h264.')
    quality=config.get('capture_quality','custom')
    mode=advanced.get('bitrate_mode','cbr' if quality=='custom' else 'qp')
    if (mode=='cbr') != (quality=='custom'):
        raise ValueError('CBR needs custom bitrate; QP/VBR/auto need a named quality preset.')
    if quality not in ('custom','medium','high','very_high','ultra'):
        raise ValueError('Unsupported capture quality.')
    rate=['-bm',mode,'-q',str(config['bitrate']) if quality=='custom' else quality]
    command=[gsr_program(config),'-w',target,'-f',str(config['fps']),'-encoder',encoder,
        '-fallback-cpu-encoding','yes' if advanced.get('fallback_cpu_encoding',False) else 'no',
        '-k',codec,*rate,'-keyint',str(advanced.get('keyint',1)),
        '-c','segment','-ffmpeg-opts','segment_time=2;segment_format=matroska;segment_list={list};reset_timestamps=1;segment_format_options=cluster_time_limit=250:flush_packets=1',
        '-write-first-frame-ts','yes','-o','{output}','-v','yes' if advanced.get('verbose',False) else 'no']
    limit=capture_resolution_limit(display,config.get('capture_resolution','native'))
    if limit:command+=['-s',f'{limit[0]}x{limit[1]}']
    for key,flag in [('framerate_mode','-fm'),('pixel_format','-pixfmt'),('color_range','-cr'),('tune','-tune')]:
        if key in advanced:command += [flag,advanced[key]]
    for key,flag in [('cursor','-cursor'),('low_power','-low-power'),('overclock','-oc'),
                     ('gl_debug','-gl-debug'),('exclude_metadata','-exclude-metadata')]:
        if key in advanced:command += [flag,'yes' if advanced[key] else 'no']
    if capture_target_base(target)=='portal':
        command+=['-restore-portal-session','yes' if advanced.get('restore_portal_session',True) else 'no']
        if 'portal_session_token_filepath' in advanced:
            command+=['-portal-session-token-filepath',str(Path(advanced['portal_session_token_filepath']).expanduser())]
    if capture_target_base(target)=='region':
        if 'region' not in advanced:raise ValueError('The region target requires capture_advanced.region.')
        r=advanced['region'];command+=['-region',f"{r['width']}x{r['height']}+{r['x']}+{r['y']}"]
    for plugin in advanced.get('plugins',[]):command+=['-p',plugin]
    if advanced.get('video_options'):command+=['-ffmpeg-video-opts',codec_options(advanced['video_options'])]
    return command,'GPU Screen Recorder'


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders():
    p=subprocess.run(['ffmpeg','-hide_banner','-encoders'],capture_output=True,text=True,timeout=10)
    return {m[1] for m in re.finditer(r'^\s*V\S*\s+(\S+)',p.stdout,re.M)} if p.returncode==0 else set()


def export_choices():
    labels=[('H.264 · CPU','libx264'),('H.265 · CPU','libx265'),('AV1 · CPU','libaom-av1')]
    vendors={d['vendor'] for d in devices()}
    if 'nvidia' in vendors:labels += [('H.264 · NVIDIA NVENC','h264_nvenc'),('H.265 · NVIDIA NVENC','hevc_nvenc'),('AV1 · NVIDIA NVENC','av1_nvenc')]
    if vendors & {'amd','intel'}:labels += [('H.264 · AMD / Intel VA-API','h264_vaapi'),('H.265 · AMD / Intel VA-API','hevc_vaapi'),('AV1 · AMD / Intel VA-API','av1_vaapi')]
    built=ffmpeg_encoders();return [(label,codec) for label,codec in labels if codec in built]


def doctor(config):
    tools={name:bool(shutil.which(name)) for name in ('ffmpeg','ffprobe','pw-dump',gsr_program(config))}
    try:info=gsr_info(gsr_program(config))
    except (OSError,RuntimeError,subprocess.SubprocessError) as e:info={'error':str(e)}
    return {'gpu_devices':devices(),'capture_capabilities':info,'tools':tools,'ffmpeg_video_encoders':sorted(ffmpeg_encoders()),'replay_storage':'RAM (tmpfs)','note':'Compiled encoders do not guarantee hardware codec support. Test the selected codec on the target GPU.'}
