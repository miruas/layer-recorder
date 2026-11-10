# SPDX-License-Identifier: GPL-3.0-only
import importlib.util,os,sys,tempfile,unittest
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
