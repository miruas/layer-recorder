# SPDX-License-Identifier: GPL-3.0-only
"""Stable, readable PipeWire stream identities and recording-window activity."""
from .identity import application_label


def stream_identity(props):
    app=props.get('application.name') or props.get('application.process.binary') or props.get('node.name','Application')
    identifiers=[str(props.get(k,'')).lower() for k in ('application.id','application.process.binary','application.name')]
    binaries=[value.rsplit('/',1)[-1] for value in identifiers]
    if any(value in {'discord','discordcanary','discordptb','vesktop','com.discordapp.discord','dev.vencord.vesktop'} or value.endswith('.discord') for value in binaries):app='Discord'
    app=application_label({'class':app}) or 'Application'
    role_hint=str(props.get('media.role','')).lower()
    technical=' '.join(str(props.get(k,'')) for k in ('media.name','node.name')).lower()
    technical_voice='voiceengine' in technical.replace(' ','').replace('_','')
    if role_hint in ('communication','phone','voip') or app=='Discord' and technical_voice:
        role,label='voice','Voice chat'
    elif role_hint in ('game','gaming'):
        role,label='game','Game audio'
    elif role_hint in ('notification','event','alert'):
        role,label='notifications','Notifications'
    elif role_hint in ('music','movie','video','production'):
        role,label='media','Media'
    elif any(word in technical for word in ('webrtc','voiceengine','voice chat')):
        role,label='voice','Voice chat'
    elif any(word in technical for word in ('notification','event-sound')):
        role,label='notifications','Notifications'
    else:
        role,label='media','Media'
    return 'app:'+app.casefold()+':'+role,{'name':app+' — '+label,'application':app,'stream_role':role,'kind':'application'}


def active_in_window(track,start,end):
    if track.meta.get('type')!='audio':return True
    intervals=getattr(track,'activity',None)
    # Legacy producers without activity metadata retain their original behavior.
    if intervals is None:return True
    intervals=list(intervals)+list(getattr(track,'presence',[]))
    return any(max(start,a)<min(end,b if b is not None else end)-.001 for a,b in intervals)


def update_activity(track,active,now):
    if not hasattr(track,'activity'):track.activity=[]
    opened=bool(track.activity) and track.activity[-1][1] is None
    if active and not opened:track.activity.append([now,None])
    elif not active and opened:track.activity[-1][1]=now


def has_audio_content(path,start,span):
    """Exclude genuinely silent application tracks, without dropping quiet signals."""
    import re,subprocess
    try:
        p=subprocess.run(['ffmpeg','-v','info','-nostats','-ss',str(max(0,start)),'-i',str(path),'-t',str(max(.01,span)),'-vn','-af','astats=metadata=0:reset=0:measure_perchannel=none:measure_overall=Peak_level','-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        if p.returncode:return True
        peaks=re.findall(r'Peak level dB:\s*([-+a-zA-Z0-9.]+)',p.stderr.decode(errors='replace'))
        return not peaks or any(peak!='-inf' for peak in peaks)
    except (OSError,subprocess.SubprocessError,ValueError):
        # Failure to analyze is never permission to discard captured media.
        return True


def update_presence(track,present,now):
    # Presence protects very short pulses that happen between running-state polls;
    # the selected audio samples, not an idle node, decide final membership.
    if not hasattr(track,'presence'):track.presence=[]
    opened=bool(track.presence) and track.presence[-1][1] is None
    if present and not opened:track.presence.append([now,None])
    elif not present and opened:track.presence[-1][1]=now
