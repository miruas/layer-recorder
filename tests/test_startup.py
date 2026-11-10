import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from katman import __main__ as cli


class ManagedStartup(unittest.TestCase):
    def test_existing_engine_is_reused(self):
        with patch.object(cli, 'request', return_value={}), patch.object(cli.subprocess, 'run') as run, patch.object(cli.subprocess, 'Popen') as spawn:
            cli.ensure_daemon()
            run.assert_not_called()
            spawn.assert_not_called()

    def test_installed_service_owns_new_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            unit=root/'systemd/user/katman.service'
            unit.parent.mkdir(parents=True)
            unit.touch()
            with patch.object(cli, 'CONFIG', root/'katman'), patch.object(cli, 'request', side_effect=[OSError(), {}]), patch.object(cli.subprocess, 'run') as run, patch.object(cli.subprocess, 'Popen') as spawn, patch.object(cli.time, 'sleep'):
                cli.ensure_daemon()
                self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'start', 'katman.service'])
                spawn.assert_not_called()

    def test_portable_launch_still_works_without_installed_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(cli, 'CONFIG', root/'katman'), patch.object(cli, 'CACHE', root), patch.object(cli, 'request', side_effect=[OSError(), {}]), patch.object(cli.subprocess, 'run') as run, patch.object(cli.subprocess, 'Popen') as spawn, patch.object(cli.time, 'sleep'):
                cli.ensure_daemon()
                run.assert_not_called()
                self.assertEqual(spawn.call_count,1)
                self.assertEqual(spawn.call_args.args[0][1:], ['-m','katman.daemon'])


if __name__ == '__main__':
    unittest.main()
