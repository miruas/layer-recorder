# SPDX-License-Identifier: GPL-3.0-only
"""GSR audio policies for PipeWire, PulseAudio and display-only sessions."""
import shutil
import subprocess

SEGMENT_OPTIONS = ('segment_time=2;segment_format=matroska;segment_list={list};'
                   'reset_timestamps=1;segment_format_options=cluster_time_limit=250:flush_packets=1')


def command(program, selector):
    return [program, '-a', selector, '-ac', 'opus', '-ab', '256', '-c', 'segment',
            '-ffmpeg-opts', SEGMENT_OPTIONS, '-write-first-frame-ts', 'yes',
            '-o', '{output}', '-v', 'no']


def audio_plan(program, info):
    """Prefer independent app roles; retain GSR device capture without PipeWire.

    PulseAudio has no GSR application-node selector. Each enumerated input and
    output monitor remains separate; application/role separation is explicitly
    unavailable in that mode. Default aliases are omitted when concrete devices
    exist so that the same sound is not captured twice.
    """
    if info.get('supports_app_audio') == 'yes' and shutil.which('pw-dump'):
        return {'mode': 'pipewire', 'tracks': [], 'warnings': []}
    warning = ('Per-application audio separation is unavailable in this session. '
               'GSR will capture separate output devices and microphones instead.')
    try:
        result = subprocess.run([program, '--list-audio-devices'], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise RuntimeError('GSR could not enumerate audio devices.')
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return {'mode': 'none', 'tracks': [], 'warnings': [
            'No GSR audio device is available. Only display sources will be captured.']}
    devices = {}
    for line in result.stdout.splitlines():
        name, separator, label = line.partition('|')
        if separator and name and not name.startswith(('katman-', 'layer-', 'gsr-')):
            devices.setdefault(name, label or name)
    concrete = {name: label for name, label in devices.items()
                if name not in ('default_input', 'default_output')}
    selected = concrete or devices
    tracks = []
    for name, label in selected.items():
        output = name == 'default_output' or name.endswith('.monitor')
        kind = 'system' if output else 'microphone'
        meta = {'type': 'audio', 'kind': kind, 'name': label,
                'audio_device': name, 'audio_codec': 'opus',
                'backend': 'GPU Screen Recorder', 'audio_provider': 'pulseaudio'}
        tracks.append({'key': 'pulse:' + name, 'meta': meta,
                       'command': command(program, name), 'selector': name})
    return {'mode': 'pulseaudio' if tracks else 'none', 'tracks': tracks,
            'warnings': [warning] if tracks else [
                'No GSR audio device is available. Only display sources will be captured.']}
