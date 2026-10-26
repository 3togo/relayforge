"""The applet package reload hook only targets a stale user's desktop process."""
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'relay_applet_restart', ROOT / 'integration/nm-applet/restart-applet.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AppletRestartTests(unittest.TestCase):
    def test_only_replaced_binary_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'proc/123'
            process.mkdir(parents=True)
            old_binary = root / 'old-nm-applet'
            new_binary = root / 'nm-applet'
            old_binary.write_bytes(b'old')
            new_binary.write_bytes(b'new')
            (process / 'exe').symlink_to(old_binary)
            with patch.object(module, 'APPLET', new_binary), patch.object(
                    module.os, 'readlink', return_value=str(new_binary) + ' (deleted)'):
                self.assertTrue(module.stale_applet(123, new_binary.stat().st_ino, root / 'proc'))
            (process / 'exe').unlink()
            (process / 'exe').symlink_to(new_binary)
            with patch.object(module, 'APPLET', new_binary):
                self.assertFalse(module.stale_applet(123, new_binary.stat().st_ino, root / 'proc'))

    def test_desktop_environment_is_filtered_for_same_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / '123'
            process.mkdir()
            (process / 'environ').write_bytes(
                b'DISPLAY=:0\0DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\0'
                b'LD_PRELOAD=/tmp/untrusted.so\0')
            account = types.SimpleNamespace(pw_uid=os.getuid(), pw_dir='/home/test')
            result = module.desktop_environment(123, account, root)
            self.assertEqual(result['DISPLAY'], ':0')
            self.assertNotIn('LD_PRELOAD', result)
            self.assertEqual(result['HOME'], '/home/test')
            other = types.SimpleNamespace(pw_uid=os.getuid() + 1, pw_dir='/home/other')
            self.assertIsNone(module.desktop_environment(123, other, root))


if __name__ == '__main__':
    unittest.main()
