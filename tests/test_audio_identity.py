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

    def test_recycled_port_ids_reconnect_and_application_links_are_passive(self):
        from unittest.mock import patch
        from katman.capture import Recorder
        r=Recorder.__new__(Recorder);r.origin=0
        t=SimpleNamespace(key='app:discord:voice',meta={'type':'audio'},name='capture-voice',links={(11,12)},activity=[],presence=[],alive=lambda:True)
        r.tracks=[t]
        node={'type':'PipeWire:Interface:Node','id':1,'info':{'state':'running','props':{'application.name':'Discord','media.class':'Stream/Output/Audio','media.role':'Communication','node.name':'Discord'}}}
        capture={'type':'PipeWire:Interface:Node','id':99,'info':{'props':{'application.name':'Layer capture','media.class':'Stream/Input/Audio','node.name':'capture-voice'}}}
        ports=[{'type':'PipeWire:Interface:Port','id':11,'info':{'props':{'node.id':1,'port.name':'output_FL','port.direction':'out'}}},{'type':'PipeWire:Interface:Port','id':12,'info':{'props':{'node.id':99,'port.name':'input_FL','port.direction':'in'}}}]
        link={'type':'PipeWire:Interface:Link','info':{'output-port-id':11,'input-port-id':12}}
        with patch('katman.capture.run') as run:
            r._discover_audio([node,capture,*ports,link]);run.assert_not_called()
            r._discover_audio([node,capture,*ports]);run.assert_called_with(['pw-link','-P','11','12'],timeout=2)

    def test_role_change_disconnects_only_the_old_owned_capture_link(self):
        from unittest.mock import patch
        from katman.capture import Recorder
        r=Recorder.__new__(Recorder);r.origin=0
        voice=SimpleNamespace(key='app:discord:voice',meta={'type':'audio'},name='capture-voice',links=set(),activity=[],presence=[],alive=lambda:True)
        media=SimpleNamespace(key='app:discord:media',meta={'type':'audio'},name='capture-media',links={(11,12)},activity=[],presence=[],alive=lambda:True);r.tracks=[voice,media]
        def node(id,name,kind,**props):return {'type':'PipeWire:Interface:Node','id':id,'info':{'state':'running','props':{'node.name':name,'media.class':kind,**props}}}
        def port(id,nid,name,d):return {'type':'PipeWire:Interface:Port','id':id,'info':{'props':{'node.id':nid,'port.name':name,'port.direction':d}}}
        objects=[node(1,'Discord','Stream/Output/Audio',**{'application.name':'Discord','media.role':'Communication'}),node(98,'capture-media','Stream/Input/Audio'),node(99,'capture-voice','Stream/Input/Audio'),port(11,1,'output_FL','out'),port(12,98,'input_FL','in'),port(13,99,'input_FL','in'),{'type':'PipeWire:Interface:Link','info':{'output-port-id':11,'input-port-id':12}}]
        with patch('katman.capture.run') as run:
            r._discover_audio(objects)
            self.assertEqual(run.call_count,2);self.assertEqual(run.call_args_list[0].args[0],['pw-link','-d','11','12']);self.assertEqual(run.call_args_list[1].args[0],['pw-link','-P','11','13'])
        self.assertFalse(media.links);self.assertEqual(voice.links,{(11,13)})
