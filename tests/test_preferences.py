# SPDX-License-Identifier: GPL-3.0-only
import copy,json,tempfile,threading,unittest
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
    def test_gsr_keys_round_trip_and_settings_preserve_unknown_fields(self):
        for sequence in ('Alt+Z','Alt+F11','Ctrl+Alt+P','Meta+Shift+F9'):self.assertEqual(gsr.encode(gsr.decode(gsr.encode(sequence))),gsr.encode(sequence))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);file=root/'config_ui';original={'main':{'show_hide_hotkey':gsr.encode('Alt+Z'),'unrelated':'keep'},'record':{'record_options':{'codec':'h264','audio_codec':'opus'}},'replay':{'time':300,'record_options':{'codec':'h264'},'save_hotkey':gsr.encode('Alt+F10')}};file.write_text(json.dumps(original))
            cfg=profile('nvidia');cfg.update(capture_codec='hevc',buffer=600,gsr_sync=True)
            with patch('katman.gsr_integration.config_path',return_value=file),patch.dict('os.environ',{'XDG_STATE_HOME':str(root/'state')}),patch('katman.gsr_integration.shutil.which',return_value=None):backup=gsr.update(cfg,{'overlay':'Ctrl+Alt+Z'})
            value=json.loads(file.read_text());self.assertEqual(value['main']['unrelated'],'keep');self.assertEqual(value['record']['record_options']['audio_codec'],'opus');self.assertEqual(value['replay']['time'],600);self.assertEqual(value['record']['record_options']['codec'],'hevc');self.assertFalse(value['record']['record_options']['show_notifications']);self.assertEqual(json.loads(backup.read_text()),original)
    def test_apply_preserves_replay_before_restart_and_never_stops_normal_recording(self):
        r=Recorder.__new__(Recorder);r.lock=threading.RLock();r.record_start=None;r.replay=True;r.running=True;r.origin=0;r.buffer_limit=300;events=[]
        r._snapshot=lambda *args:events.append('save') or 'preserved.katman';r._stop=lambda:events.append('stop');r._start=lambda:events.append('start');r.status=lambda:{'replay':r.replay}
        result=r.reload_settings();self.assertEqual(events,['save','stop','start']);self.assertEqual(result['preserved_project'],'preserved.katman');events.clear();r.record_start=1
        with self.assertRaises(RuntimeError):r.reload_settings()
        self.assertEqual(events,[])
    def test_save_presets_route_through_the_shared_bridge(self):
        from katman import bridge
        cfg=profile('auto');cfg.update(buffer=600,replay_short=30,replay_long=180)
        with patch('katman.__main__.ensure_daemon'),patch('katman.bridge.settings',return_value=cfg),patch('katman.bridge.request',return_value={}) as request:
            bridge.mirror('save-1');request.assert_called_with('save-replay',seconds=30);bridge.mirror('save-5');request.assert_called_with('save-replay',seconds=180)
