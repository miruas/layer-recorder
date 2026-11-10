# SPDX-License-Identifier: GPL-3.0-only
import unittest
from types import SimpleNamespace
from katman.common import audio_sources
from katman.audio_identity import active_in_window,update_activity
class AudioStreams(unittest.TestCase):
    def test_discord_voice_and_media_remain_independent(self):
        def node(id,role,state='running'):
            return {'type':'PipeWire:Interface:Node','id':id,'info':{'state':state,'props':{'application.name':'Discord','media.class':'Stream/Output/Audio','media.role':role}}}
        groups=audio_sources([node(1,'Communication'),node(2,'Movie'),node(3,'Movie','idle')])
        self.assertEqual(len(groups),2)
        self.assertEqual(groups['app:discord:voice']['name'],'Discord — Voice chat')
        self.assertEqual(groups['app:discord:media']['nodes'],[2,3])
        self.assertTrue(groups['app:discord:media']['active'])
    def test_old_app_absent_from_recent_replay_but_retained_in_older_recording(self):
        t=SimpleNamespace(meta={'type':'audio'},activity=[])
        update_activity(t,True,0);update_activity(t,False,10)
        self.assertFalse(active_in_window(t,60,120));self.assertTrue(active_in_window(t,0,120))
        update_activity(t,True,100)
        self.assertTrue(active_in_window(t,60,120));self.assertFalse(active_in_window(t,11,99))
    def test_idle_stream_is_not_an_active_recording_source(self):
        groups=audio_sources([{'type':'PipeWire:Interface:Node','id':1,'info':{'state':'idle','props':{'application.name':'Firefox','media.class':'Stream/Output/Audio'}}}])
        self.assertFalse(next(iter(groups.values()))['active'])

    def test_layer_preview_never_recaptures_itself(self):
        group=audio_sources([{'type':'PipeWire:Interface:Node','id':1,'info':{'state':'running','props':{'application.name':'Layer','media.class':'Stream/Output/Audio'}}}])
        self.assertEqual(group,{})
    def test_audio_content_is_scoped_to_the_saved_range(self):
        import tempfile
        from pathlib import Path
        from katman.common import run
        from katman.audio_identity import has_audio_content
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.wav'
            run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.3','-af','apad=whole_dur=1','-c:a','pcm_s16le',path])
            self.assertTrue(has_audio_content(path,0,.2));self.assertFalse(has_audio_content(path,.5,.4))
            self.assertTrue(has_audio_content(Path(directory)/'missing.wav',0,1))

    def test_short_pulse_between_state_polls_is_not_dropped(self):
        from katman.audio_identity import update_presence
        t=SimpleNamespace(meta={'type':'audio'},activity=[])
        update_presence(t,True,0);self.assertTrue(active_in_window(t,1,2))
        update_presence(t,False,3);self.assertFalse(active_in_window(t,5,6))

    def test_high_frequency_and_opposite_phase_stereo_are_retained(self):
        import tempfile
        from pathlib import Path
        from katman.common import run
        from katman.audio_identity import has_audio_content
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'high.wav'
            run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=12000:duration=0.2:sample_rate=48000','-af','pan=stereo|c0=c0|c1=-1*c0','-c:a','pcm_s16le',path])
            self.assertTrue(has_audio_content(path,0,.2))

    def test_role_metadata_outranks_media_titles(self):
        from katman.audio_identity import stream_identity
        _,identity=stream_identity({'application.name':'Discord','media.role':'Movie','media.name':'A private voice chat video title'})
        self.assertEqual(identity['name'],'Discord — Media')

    def test_discord_helper_names_resolve_to_the_parent_application(self):
        from katman.audio_identity import stream_identity
        _,voice=stream_identity({'application.name':'WEBRTC VoiceEngine','application.process.binary':'Discord','media.role':'Music','media.name':'WEBRTC VoiceEngine'})
        _,media=stream_identity({'application.name':'Chromium','application.id':'com.discordapp.Discord','media.role':'Movie'})
        self.assertEqual(voice['name'],'Discord — Voice chat');self.assertEqual(media['name'],'Discord — Media')
