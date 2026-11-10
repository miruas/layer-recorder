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
            if key in ('gsr_version','fork_name','fork_version','upstream_revision'):result[key]=value
        elif section=='gpu_info' and line.startswith('vendor|'):result['vendor']=line.split('|',1)[1]
        elif section=='video_codecs':result['codecs'].append(line.strip())
    return result


def capture_command(display,config,direct,info,helper,inventory=None):
    vendor=config['gpu_vendor'];backend=config['capture_backend'];codec=config['capture_codec']
    same_vendor=vendor=='auto' or vendor==info.get('vendor')
    use_gsr=backend!='wf-recorder' and display['name'] in direct and same_vendor
    if backend=='gsr' and not use_gsr:raise RuntimeError('GSR cannot capture this output with the requested GPU profile. Use auto or the Wayland backend.')
    if use_gsr:
        if codec not in info.get('codecs',[]):raise RuntimeError('The capture GPU does not advertise '+codec+' encoding. Choose a supported capture_codec.')
        return [gsr_program(config),'-w',display['name'],'-f',str(config['fps']),'-encoder','gpu','-fallback-cpu-encoding','no','-k',codec,'-bm','cbr','-q',str(config['bitrate']),'-keyint','1','-c','segment','-ffmpeg-opts','segment_time=2;segment_format=matroska;segment_list={list};reset_timestamps=1;segment_format_options=cluster_time_limit=250:flush_packets=1','-write-first-frame-ts','yes','-o','{output}','-v','no'], 'GPU Screen Recorder'
    inventory=devices() if inventory is None else inventory
    selected=vendor if vendor!='auto' else info.get('vendor')
    if selected not in ('nvidia','amd','intel'):
        selected=next((d['vendor'] for d in inventory if d['vendor'] in ('nvidia','amd','intel')),None)
    if not selected:raise RuntimeError('No supported GPU detected. Check NVIDIA, Mesa or Intel media drivers.')
    if vendor!='auto' and not any(d['vendor']==vendor for d in inventory):raise RuntimeError('The requested '+vendor+' GPU was not found.')
    program=config['wf_recorder_binary'] or str(helper)
    if not (Path(program).is_file() or shutil.which(program)):raise RuntimeError('Build the Layer Wayland helper with tools/build-native.py wf-recorder, or configure wf_recorder_binary.')
    encoder={'h264':'h264','hevc':'hevc','av1':'av1'}[codec]+('_nvenc' if selected=='nvidia' else '_vaapi')
    args=[program,'-o',display['name'],'-c',encoder,'-p','g='+str(config['fps']),'-p','b='+str(config['bitrate'])+'k','-b','0','-D','-r',str(config['fps']),'-m','segment','-f','{output}','-y']
    if selected=='nvidia':args+=['-p','preset=p3']
    else:args+=['-d',render_device(config,inventory)]
    if config['wf_no_dmabuf']:args+=['--no-dmabuf']
    return args,'Wayland / '+('NVENC' if selected=='nvidia' else 'VA-API')


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
    tools={name:bool(shutil.which(name)) for name in ('ffmpeg','ffprobe','pw-record','pw-link','pw-dump','hyprctl',gsr_program(config))}
    try:info=gsr_info(gsr_program(config))
    except (OSError,RuntimeError,subprocess.SubprocessError) as e:info={'error':str(e)}
    return {'gpu_devices':devices(),'capture_capabilities':info,'tools':tools,'ffmpeg_video_encoders':sorted(ffmpeg_encoders()),'replay_storage':'RAM (tmpfs)','note':'Compiled encoders do not guarantee hardware codec support. Test the selected codec on the target GPU.'}
