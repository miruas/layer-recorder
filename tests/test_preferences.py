# SPDX-License-Identifier: GPL-3.0-only
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from katman.configuration import profile,validate_config
from katman import shortcuts,gsr_integration as gsr
from katman.capture import Recorder

class Preferences(unittest.TestCase):
    def test_replay_presets_and_h265_validate(self):
        cfg=profile('nvidia');cfg.update(buffer=600,replay_short=30,replay_long=180,capture_codec='hevc',export_codec='hevc_nvenc');self.assertEqual(validate_config(cfg)['capture_codec'],'hevc')
        for change in ({'replay_short':200},{'replay_long':700},{'buffer':20}):
            with self.assertRaises(ValueError):validate_config({**cfg,**change})
    def test_duplicate_layer_shortcuts_are_rejected(self):
        cfg=profile('auto');cfg['shortcuts']['record']=cfg['shortcuts']['ui']
        with self.assertRaises(ValueError):validate_config(cfg)
    def test_foreign_binding_is_never_overwritten(self):
        cfg=profile('auto');bindings=[{'modmask':72,'key':'Z','dispatcher':'exec','arg':'other-application'}]
        with patch('katman.shortcuts.inventory',return_value=bindings),patch('katman.shortcuts.subprocess.run') as run:
            with self.assertRaises(ValueError):shortcuts.sync(cfg)
            run.assert_not_called()
    def test_move_owned_shortcut_and_preserve_foreign_actions(self):
        cfg=profile('auto');cfg['shortcuts']={key:'' for key in shortcuts.ACTIONS};cfg['shortcuts']['ui']='Meta+Alt+P'
        bindings=[{'modmask':72,'key':'Z','dispatcher':'exec','arg':shortcuts.command('ui')},{'modmask':4,'key':'Q','dispatcher':'exec','arg':'other-application'}]
        import subprocess
        with patch('katman.shortcuts.inventory',return_value=bindings),patch('katman.shortcuts.subprocess.run',return_value=subprocess.CompletedProcess([],0,'ok','')) as run:
            shortcuts.sync(cfg)
        self.assertEqual(len(run.call_args_list),2);self.assertEqual(run.call_args_list[0].args[0][2],'unbind');self.assertIn('SUPER',run.call_args_list[1].args[0][3]);self.assertIn('P, exec,',run.call_args_list[1].args[0][3])
    def test_lua_shortcuts_fall_back_and_are_recognized_without_rebinding(self):
        import subprocess
        cfg=profile('auto');cfg['shortcuts']={key:'' for key in shortcuts.ACTIONS};cfg['shortcuts']['ui']='Meta+Alt+P'
        responses=[subprocess.CompletedProcess([],0,"keyword can't work with non-legacy parsers. Use eval.",''),subprocess.CompletedProcess([],0,'ok','')]
        with patch('katman.shortcuts.inventory',return_value=[]),patch('katman.shortcuts.subprocess.run',side_effect=responses) as run:
            shortcuts.sync(cfg)
        script=run.call_args_list[1].args[0];self.assertEqual(script[1],'eval');self.assertIn('allow_input_capture=true',script[2]);self.assertIn('Layer shortcut: ',script[2])
        binding={'modmask':72,'key':'P','dispatcher':'__lua','arg':'123','description':'Layer shortcut: '+shortcuts.command('ui')}
        with patch('katman.shortcuts.inventory',return_value=[binding]),patch('katman.shortcuts.subprocess.run') as run:
            shortcuts.sync(cfg);run.assert_not_called()

    def test_gsr_keys_round_trip_and_settings_preserve_unknown_fields(self):
        for sequence in ('Alt+Z','Alt+F11','Ctrl+Alt+P','Meta+Shift+F9'):self.assertEqual(gsr.encode(gsr.decode(gsr.encode(sequence))),gsr.encode(sequence))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);file=root/'config_ui';original={'main':{'show_hide_hotkey':gsr.encode('Alt+Z'),'unrelated':'keep'},'record':{'record_options':{'codec':'h264','audio_codec':'opus'}},'replay':{'time':300,'record_options':{'codec':'h264'},'save_hotkey':gsr.encode('Alt+F10')}};file.write_text(json.dumps(original))
            cfg=profile('nvidia');cfg.update(capture_codec='hevc',buffer=600,gsr_sync=True)
            with patch('katman.gsr_integration.config_path',return_value=file),patch.dict('os.environ',{'XDG_STATE_HOME':str(root/'state')}),patch('katman.gsr_integration.shutil.which',return_value=None):backup=gsr.update(cfg,{'overlay':'Ctrl+Alt+Z'})
            value=json.loads(file.read_text());self.assertEqual(value['main']['unrelated'],'keep');self.assertEqual(value['record']['record_options']['audio_codec'],'opus');self.assertEqual(value['replay']['time'],600);self.assertEqual(value['record']['record_options']['codec'],'hevc');self.assertFalse(value['record']['record_options']['show_notifications']);self.assertEqual(json.loads(backup.read_text()),original)
    def test_capture_codecs_preserve_hdr_and_auto_but_reject_unknown(self):
        for codec in ('auto','h264','hevc','hevc_10bit','hevc_hdr','av1','av1_10bit','av1_hdr'):
            self.assertEqual(validate_config({'capture_codec':codec})['capture_codec'],codec)
        with self.assertRaises(ValueError):validate_config({'capture_codec':'vp9'})
    def test_capture_quality_presets_and_custom_validate(self):
        for quality in ('custom','medium','high','very_high','ultra'):
            self.assertEqual(validate_config({'capture_quality':quality})['capture_quality'],quality)
        with self.assertRaises(ValueError):validate_config({'capture_quality':'lossless'})
    def test_gsr_preference_flags_and_large_durations(self):
        cfg=validate_config({'buffer':86400,'replay_short':7200,'replay_long':43200,'gsr_preferences':False,'gsr_config':'/tmp/gsr-config'})
        self.assertEqual((cfg['buffer'],cfg['replay_short'],cfg['replay_long']),(86400,7200,43200))
        self.assertFalse(cfg['gsr_preferences'])
        for values in ({'buffer':1},{'replay_short':86401},{'gsr_preferences':'yes'},{'gsr_config':1},{'buffer':86400,'replay_long':86401}):
            with self.subTest(values=values),self.assertRaises(ValueError):validate_config(values)
    def test_reload_settings_forwards_the_facade_request(self):
        r=Recorder.__new__(Recorder)
        with patch('katman.capture.request',return_value={'ok':True}) as request:
            self.assertEqual(r.reload_settings(),{'ok':True})
            request.assert_called_once_with('reload-settings')
    def test_save_presets_route_through_the_shared_bridge(self):
        from katman import bridge
        cfg=profile('auto');cfg.update(buffer=600,replay_short=30,replay_long=180)
        with patch('katman.__main__.ensure_daemon'),patch('katman.bridge.settings',return_value=cfg),patch('katman.bridge.request',return_value={}) as request:
            bridge.mirror('save-1');request.assert_called_with('save-replay',seconds=30);bridge.mirror('save-5');request.assert_called_with('save-replay',seconds=180)
