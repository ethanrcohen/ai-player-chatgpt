from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from bridge.play import configure_local_rcon

class LauncherConfigTests(unittest.TestCase):
    def test_only_local_rcon_changes_and_backup_preserved(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.ini'
            original='[other]\n; local-rcon-socket=0.0.0.0:0\n; local-rcon-password=\nfoo=bar\n[graphics]\nvalue=1\n'
            path.write_text(original)
            cfg=SimpleNamespace(rcon=SimpleNamespace(port=27015,password='test-password'))
            configure_local_rcon(path,cfg)
            result=path.read_text()
            self.assertIn('local-rcon-socket=127.0.0.1:27015',result)
            self.assertIn('local-rcon-password=test-password',result)
            self.assertIn('foo=bar\n[graphics]\nvalue=1',result)
            configure_local_rcon(path,cfg)
            self.assertEqual(path.read_text(),result)
            self.assertEqual(path.with_name('config.ini.before-ai-rcon').read_text(),original)
