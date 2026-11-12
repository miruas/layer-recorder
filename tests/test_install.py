# SPDX-License-Identifier: GPL-3.0-only
import importlib.util,os,sys,tempfile,unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('layer_install',Path(__file__).resolve().parents[1]/'install.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class UserInstallation(unittest.TestCase):
    def test_install_repeat_and_uninstall_are_isolated_and_preserve_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source with space%';source.mkdir();home=root/'home';home.mkdir();config=root/'config';data=root/'data'
            env={'XDG_CONFIG_HOME':str(config),'XDG_DATA_HOME':str(data)}
            def run(command,**kwargs):return __import__('subprocess').CompletedProcess(command,1 if 'is-active' in command else 0,'','')
            with patch.object(installer,'ROOT',source),patch.object(Path,'home',return_value=home),patch.dict(os.environ,env),patch.object(installer.subprocess,'run',side_effect=run):
                for _ in range(2):
                    with patch.object(sys,'argv',['install.py','--profile','amd']):installer.main()
                settings=config/'katman/settings.json';before=settings.read_bytes();unit=(config/'systemd/user/katman.service').read_text();self.assertIn('space%%',unit)
                with patch.object(sys,'argv',['install.py','--uninstall']):installer.main()
                self.assertFalse((home/'.local/bin/layer').exists());self.assertEqual(settings.read_bytes(),before)
    def test_unrelated_launcher_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);home=root/'home';file=home/'.local/bin/layer';file.parent.mkdir(parents=True);file.write_text('unrelated executable')
            with patch.object(Path,'home',return_value=home),patch.dict(os.environ,{'XDG_CONFIG_HOME':str(root/'config'),'XDG_DATA_HOME':str(root/'data')}),patch.object(installer.subprocess,'run'),patch.object(sys,'argv',['install.py']):
                with self.assertRaises(SystemExit):installer.main()
            self.assertEqual(file.read_text(),'unrelated executable')

    def test_non_systemd_login_startup_needs_no_qt_and_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source with spaces'
            source.mkdir()
            home, config, data = root/'home', root/'config', root/'data'
            home.mkdir()
            unrelated = config/'autostart/another-application.desktop'
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text('[Desktop Entry]\nName=Another application\n')
            original = unrelated.read_bytes()
            def run(command, **kwargs):
                if command[0] == 'systemctl':
                    raise FileNotFoundError('systemctl is not installed')
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(installer, 'ROOT', source), \
                 patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config),
                                         'XDG_DATA_HOME': str(data)}), \
                 patch.dict(sys.modules, {'PySide6': None}), \
                 patch.object(installer.shutil, 'which', return_value=None), \
                 patch.object(installer.subprocess, 'run', side_effect=run):
                with patch.object(sys, 'argv', ['install.py', '--enable']):
                    installer.main()
                autostart = config/'autostart/layer-recorder.desktop'
                startup = autostart.read_text()
                self.assertIn('Exec="'+str(source/'run.sh')+'" autostart', startup)
                self.assertIn('Terminal=false', startup)
                self.assertIn('NoDisplay=true', startup)
                settings = config/'katman/settings.json'
                saved_settings = settings.read_bytes()
                with patch.object(sys, 'argv', ['install.py', '--uninstall']):
                    installer.main()
            self.assertFalse(autostart.exists())
            self.assertFalse((home/'.local/bin/layer').exists())
            self.assertFalse((config/'systemd/user/katman.service').exists())
            self.assertEqual(unrelated.read_bytes(), original)
            self.assertEqual(settings.read_bytes(), saved_settings)

    def test_non_systemd_uninstall_refuses_foreign_autostart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, config, data = root/'home', root/'config', root/'data'
            home.mkdir()
            autostart = config/'autostart/layer-recorder.desktop'
            autostart.parent.mkdir(parents=True)
            autostart.write_text('[Desktop Entry]\nExec=unrelated-program\n')
            before = autostart.read_bytes()
            with patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config),
                                         'XDG_DATA_HOME': str(data)}), \
                 patch.object(installer.subprocess, 'run', side_effect=FileNotFoundError()), \
                 patch.object(sys, 'argv', ['install.py', '--uninstall']):
                with self.assertRaises(SystemExit):
                    installer.main()
            self.assertEqual(autostart.read_bytes(), before)

class BundledRecorderPath(unittest.TestCase):
    def test_launcher_prefers_bundled_recorder_and_retains_path_fallback(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, fallback = root/'source with spaces', root/'system-bin'
            source.mkdir(); fallback.mkdir(); (source/'bin').mkdir()
            shutil.copy2(Path(__file__).resolve().parents[1]/'run.sh', source/'run.sh')
            probe = root/'probe-python'
            probe.write_text('#!/bin/sh\ncommand -v gpu-screen-recorder\n')
            probe.chmod(0o755)
            bundled, system = source/'bin/gpu-screen-recorder', fallback/'gpu-screen-recorder'
            for binary in (bundled, system):
                binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
            env = dict(os.environ, PATH=str(fallback)+':/usr/bin:/bin', LAYER_PYTHON=str(probe))
            self.assertEqual(subprocess.check_output(['sh', str(source/'run.sh'), 'overlay'], env=env, text=True).strip(), str(bundled))
            bundled.unlink()
            self.assertEqual(subprocess.check_output(['sh', str(source/'run.sh'), 'overlay'], env=env, text=True).strip(), str(system))

    def test_overlay_service_uses_bundled_engine_before_system_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, home, config, data = root/'source with spaces', root/'home', root/'config', root/'data'
            home.mkdir(); (source/'overlay/bin').mkdir(parents=True)
            (source/'overlay/bin/gsr-ui').write_text('test helper')
            (config/'systemd/user').mkdir(parents=True)
            (config/'systemd/user/gsr-ui.service').write_text('[Service]\nExecStart=/usr/bin/gsr-ui\n')
            with patch.object(installer, 'ROOT', source), patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'XDG_CONFIG_HOME':str(config), 'XDG_DATA_HOME':str(data)}), \
                 patch.object(installer.shutil, 'which', return_value=None), \
                 patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')), \
                 patch.object(sys,'argv',['install.py','--overlay']):
                installer.main()
            expected = 'Environment='+installer.systemd_quote('PATH='+str(source/'bin')+':'+str(home/'.local/bin')+':/usr/local/bin:/usr/bin:/bin')
            self.assertIn(expected, (config/'systemd/user/gsr-ui.service.d/layer.conf').read_text())
            self.assertIn(expected, (config/'systemd/user/katman.service').read_text())
