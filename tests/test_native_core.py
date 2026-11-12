# SPDX-License-Identifier: GPL-3.0-only
"""Real Rust socket/lifecycle tests with explicitly synthetic CPU video, never the desktop."""
import concurrent.futures,json,os,shutil,socket,stat,subprocess,tempfile,time,unittest
from pathlib import Path
from katman.common import APP
from katman.configuration import profile

@unittest.skipUnless((APP/'bin/layer-recorderd').is_file(),'Build the Rust core with tools/build-native.py recorder')
class NativeCore(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='layer-core-qa-');self.root=Path(self.tmp.name);tools=self.root/'tools';tools.mkdir();runtime=self.root/'run';runtime.mkdir(mode=0o700)
        self.endpoint=runtime/'katman/control.sock';self.library=self.root/'recordings'
        (tools/'pw-dump').write_text('#!/bin/sh\nprintf "[]\\n"\nexec sleep 120\n');(tools/'pw-dump').chmod(0o755)
        gsr=tools/'synthetic-gsr';gsr.write_text('''#!/usr/bin/python3
import os,sys,time
from pathlib import Path
args=sys.argv[1:]
if '--info' in args:
 print('section=system_info\\nlayer_audio_node_selector|yes\\nsupports_app_audio|yes\\nsection=gpu_info\\nvendor|nvidia\\nsection=video_codecs\\nh264\\nhevc');raise SystemExit
if '--list-monitors' in args:
 inventory=Path(__file__).with_name('display-inventory.txt')
 text=inventory.read_text() if inventory.exists() else 'QA-1|64x64'
 if text=='SLOW':time.sleep(4);text='QA-1|64x64'
 print(text);raise SystemExit
output=args[args.index('-o')+1];options=args[args.index('-ffmpeg-opts')+1];listing=next(x.split('=',1)[1] for x in options.split(';') if x.startswith('segment_list='))
Path(output+'.ts').write_text('CLOCK_MONOTONIC\\n'+str(int(time.monotonic()*1e6))+'\\n')
# Test-only source: encode generated pixels on CPU; never read a display/device.
os.execvp('ffmpeg',['ffmpeg','-hide_banner','-loglevel','error','-y','-re','-f','lavfi','-i','color=blue:s=64x64:r=10','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-threads','1','-g','10','-f','segment','-segment_time','2','-segment_format_options','cluster_time_limit=250:flush_packets=1','-segment_list',listing,'-segment_list_type','csv','-reset_timestamps','1',output])
''');gsr.chmod(0o755)
        cfg=profile('nvidia');cfg.update(library=str(self.library),gsr_binary=str(gsr),shortcuts_enabled=False)
        config=self.root/'settings.json';config.write_text(json.dumps(cfg));env=os.environ.copy();env.update(PATH=str(tools)+os.pathsep+env['PATH'],XDG_RUNTIME_DIR=str(runtime),XDG_CACHE_HOME=str(self.root/'cache'),XDG_CONFIG_HOME=str(self.root/'config'),LAYER_CONFIG=str(config));env.pop('HYPRLAND_INSTANCE_SIGNATURE',None)
        self.log=open(self.root/'daemon.log','w');self.process=subprocess.Popen([str(APP/'bin/layer-recorderd'),'--root',str(APP)],env=env,stdout=self.log,stderr=self.log)
        for _ in range(100):
            if self.endpoint.exists():break
            if self.process.poll() is not None:self.fail((self.root/'daemon.log').read_text())
            time.sleep(.05)
        self.assertTrue(self.endpoint.exists())
    def tearDown(self):
        try:
            if self.process.poll() is None:self.call('shutdown')
        except Exception:self.process.terminate()
        try:self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.log.close();self.tmp.cleanup()
    def raw(self,message):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(20);s.connect(str(self.endpoint));s.sendall(json.dumps(message).encode()+b'\n');data=b''
            while not data.endswith(b'\n'):
                block=s.recv(65536)
                if not block:raise RuntimeError('Control connection closed')
                data+=block
        return json.loads(data)
    def call(self,action,**values):
        reply=self.raw(dict(action=action,**values))
        if not reply['ok']:raise RuntimeError(reply['error'])
        return reply['data']
    def project(self,path):
        p=Path(path);v=json.loads(p.read_text());self.assertEqual(v['format'],'katman');self.assertEqual(v['version'],1);self.assertEqual(v['warnings'],[]);self.assertEqual(v['sources'][0]['width'],64)
        source=v['sources'][0];media=p.parent/source['file'];self.assertTrue(media.is_file());self.assertTrue(media.resolve().is_relative_to(p.parent.resolve()));return p,v
    def test_capture_can_start_without_pipewire_application_support(self):
        program = self.root / 'tools/synthetic-gsr'
        source = program.read_text().replace('supports_app_audio|yes', 'supports_app_audio|no')
        source = source.replace("if '--list-monitors' in args:", "if '--list-audio-devices' in args:raise SystemExit\nif '--list-monitors' in args:")
        program.write_text(source)
        status = self.call('toggle-replay')
        self.assertTrue(status['running'])
        self.assertEqual(status['audio_mode'], 'none')
        self.assertTrue(status['warnings'])
        self.assertEqual([s['type'] for s in status['sources']], ['video'])

    def test_wire_validation_and_shared_record_replay_lifecycle(self):
        self.assertEqual(stat.S_IMODE(self.endpoint.stat().st_mode),0o600)
        for message in [{'action':'invalid'},{'action':'set-capture-state','kind':'record','enabled':'false'},{'action':'save-replay','seconds':True}]:self.assertFalse(self.raw(message)['ok'])
        start=self.call('set-capture-state',kind='replay',enabled=True);self.assertEqual(start['engine'],'rust-gsr');ids=[s['id'] for s in start['sources']]
        again=self.call('set-capture-state',kind='replay',enabled=True);self.assertEqual(ids,[s['id'] for s in again['sources']])
        self.call('set-capture-state',kind='record',enabled=True);self.call('set-capture-state',kind='record',enabled=True)
        with self.assertRaisesRegex(RuntimeError,'ordinary recording'):self.call('reload-settings')
        self.call('set-capture-state',kind='replay',enabled=False);self.assertTrue(self.call('status')['recording']);time.sleep(1.1)
        stopped=self.call('set-capture-state',kind='record',enabled=False);self.assertFalse(stopped['running']);self.project(stopped['saved']);self.assertFalse(self.call('set-capture-state',kind='record',enabled=False)['recording'])
    def test_replay_save_keeps_status_responsive_and_reload_preserves_media(self):
        start=self.call('toggle-replay');time.sleep(1.1)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            save=pool.submit(self.call,'save-replay',seconds=60);time.sleep(.1);begin=time.monotonic();status=self.call('status');self.assertLess(time.monotonic()-begin,.5);self.assertTrue(status['replay']);path,v=self.project(save.result(timeout=15))
        self.assertLess(v['duration'],60);self.assertEqual(status['buffer_storage'],'ram');self.assertTrue(Path(start['buffer_directory']).is_relative_to('/dev/shm'))
        changed=self.call('reload-settings');self.assertTrue(changed['replay']);self.project(changed['preserved_project']);self.assertTrue(path.is_file());self.assertNotEqual(changed['sources'][0]['id'],start['sources'][0]['id'])
    def test_pause_cuts_ordinary_timeline_without_stopping_ram_replay(self):
        self.call('set-capture-state',kind='replay',enabled=True)
        self.call('set-capture-state',kind='record',enabled=True);time.sleep(.7)
        first=self.call('set-paused',enabled=True)
        self.assertTrue(first['paused']);self.assertTrue(first['replay'])
        duration=first['record_seconds'];time.sleep(.7)
        again=self.call('set-paused',enabled=True)
        self.assertAlmostEqual(again['record_seconds'],duration,places=2)
        self.assertGreater(again['buffer_seconds'],first['buffer_seconds']+.5)
        self.call('set-paused',enabled=False);time.sleep(.7)
        stopped=self.call('set-capture-state',kind='record',enabled=False)
        self.assertTrue(stopped['replay']);self.assertFalse(stopped['paused'])
        p,project=self.project(stopped['saved'])
        self.assertEqual(len(project['sections']),2)
        self.assertLess(project['recorded_duration'],project['source_duration']-.5)
        self.assertAlmostEqual(project['recorded_duration'],sum(v['end']-v['start'] for v in project['sections']),places=5)
        from katman.export import validate
        validate(project)
        from katman.export import export_project
        rendered=p.parent/'paused-timeline.mp4'
        export_project(p,rendered)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-of','json',str(rendered)]))
        self.assertAlmostEqual(float(probe['format']['duration']),project['recorded_duration'],delta=.2)
        from katman.studio_model import from_project
        studio=from_project(project)
        lengths=[sum(c['duration'] for c in t['clips']) for t in studio['tracks'] if t['kind']=='video']
        self.assertTrue(lengths);self.assertAlmostEqual(lengths[0],project['recorded_duration'],delta=.25)
        replay_path=self.call('save-replay',seconds=60)
        _,replay=self.project(replay_path)
        self.assertEqual(replay['sections'],[])
        self.assertGreater(replay['duration'],project['recorded_duration']+.5)

    def test_pause_validation_and_stopping_while_paused(self):
        self.assertFalse(self.raw({'action':'set-paused','enabled':'true'})['ok'])
        self.assertFalse(self.raw({'action':'set-paused','enabled':True})['ok'])
        self.call('toggle-record');time.sleep(1.)
        self.call('toggle-pause');time.sleep(.6)
        stopped=self.call('toggle-record');self.assertFalse(stopped['paused'])
        _,project=self.project(stopped['saved'])
        self.assertEqual(len(project['sections']),1)
        self.assertLess(project['recorded_duration'],project['source_duration']-.4)

    def test_display_hotplug_and_mode_changes_preserve_other_sources_and_past_media(self):
        inventory=self.root/'tools/display-inventory.txt'
        inventory.write_text('QA-1|64x64')
        start=self.call('toggle-replay');first=start['sources'][0]['id']
        def wait_for(predicate):
            until=time.monotonic()+9
            while time.monotonic()<until:
                status=self.call('status')
                videos=[v for v in status['sources'] if v['type']=='video' and v['active']]
                if predicate(videos):return videos
                time.sleep(.15)
            self.fail('Display inventory did not reconcile: '+json.dumps(status))
        time.sleep(.8);inventory.write_text('QA-1|64x64\nQA-2|64x64')
        videos=wait_for(lambda rows:len(rows)==2)
        second=next(v['id'] for v in videos if v['name']=='QA-2')
        self.assertEqual(next(v['id'] for v in videos if v['name']=='QA-1'),first)
        inventory.write_text('QA-1|128x64\nQA-2|64x64')
        videos=wait_for(lambda rows:any(v['name']=='QA-1' and v['width']==128 and v['id']!=first for v in rows))
        # Synthetic side-by-side layout also moves QA-2 when QA-1 widens.
        # Its old generation must remain recoverable in the final snapshot.
        generation=next(v['id'] for v in videos if v['name']=='QA-1')
        inventory.write_text('QA-2|64x64')
        wait_for(lambda rows:len(rows)==1 and rows[0]['name']=='QA-2')
        inventory.write_text('QA-1|128x64\nQA-2|64x64')
        videos=wait_for(lambda rows:len(rows)==2)
        self.assertNotEqual(next(v['id'] for v in videos if v['name']=='QA-1'),generation)
        time.sleep(.8)
        saved=Path(self.call('save-replay',seconds=60));project=json.loads(saved.read_text())
        self.assertTrue(all((saved.parent/v['file']).is_file() for v in project['sources']))
        self.assertTrue(all('the capture source stopped' not in warning for warning in project['warnings']))
        ids={v['id'] for v in project['sources']}
        self.assertIn(first,ids);self.assertIn(second,ids)
        self.assertTrue(self.call('status')['replay'])
        self.assertTrue(all('hotplug_fingerprint' not in v for v in project['sources']))

    def test_slow_display_inventory_does_not_block_recording_controls(self):
        self.call('toggle-replay')
        (self.root/'tools/display-inventory.txt').write_text('SLOW')
        time.sleep(2.5)
        before=time.monotonic();status=self.call('toggle-replay')
        self.assertLess(time.monotonic()-before,1.)
        self.assertFalse(status['running'])

    def test_too_short_paused_recording_can_resume_without_losing_capture(self):
        self.call('toggle-record');self.call('set-paused',enabled=True)
        time.sleep(.6)
        with self.assertRaisesRegex(RuntimeError,'too short'):self.call('toggle-record')
        status=self.call('status');self.assertTrue(status['running']);self.assertTrue(status['paused'])
        self.call('set-paused',enabled=False);time.sleep(.8)
        result=self.call('toggle-record');self.assertTrue(Path(result['saved']).is_file())

    def test_gsr_recording_inside_replay_and_stream_has_its_own_lifecycle(self):
        self.call('gsr-action',command='replay-on')
        state=self.call('gsr-action',command='record-on-replay')
        self.assertEqual(state['gsr_mode'],'replay');self.assertTrue(state['recording'])
        self.call('gsr-action',command='record-on-replay');time.sleep(.8)
        stopped=self.call('gsr-action',command='record-off')
        self.assertFalse(stopped['recording']);self.assertTrue(stopped['replay'])
        self.assertTrue(Path(stopped['last_project']).is_file())
        self.call('gsr-action',command='replay-off')
        streaming=self.call('gsr-action',command='stream-on')
        self.assertEqual(streaming['gsr_mode'],'stream');self.assertFalse(streaming['running'])
        self.call('gsr-action',command='record-on-stream');time.sleep(.8)
        stopped=self.call('gsr-action',command='stream-off')
        self.assertFalse(stopped['recording']);self.assertFalse(stopped['running'])
        self.assertEqual(stopped['gsr_mode'],'idle')
        self.assertTrue(Path(stopped['last_project']).is_file())

    def test_parent_stream_stop_does_not_stop_an_unrelated_standalone_recording(self):
        self.call('toggle-record');time.sleep(.7)
        self.call('gsr-action',command='stream-on')
        self.assertTrue(self.call('gsr-action',command='stream-off')['recording'])
        result=self.call('toggle-record');self.assertTrue(Path(result['saved']).is_file())

    def test_shutdown_saves_an_active_ordinary_recording(self):
        self.call('toggle-record');time.sleep(1.1);self.assertEqual(self.call('shutdown'),'stopped');self.process.wait(timeout=20)
        paths=list(self.library.rglob('project.katman'));self.assertEqual(len(paths),1);self.project(paths[0]);self.assertEqual(self.process.returncode,0)
