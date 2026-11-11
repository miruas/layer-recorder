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
