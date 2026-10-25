"""Fresh-install interface selection and package-upgrade interface repair.

Both commands are pure file/sysfs operations: no bus, no root and no adapter.
"""
import re
import subprocess
import tempfile
from pathlib import Path
import unittest
from test_dbus import BINARY


class Bench:
    def __init__(self, root):
        self.root = Path(root)
        self.net = self.root / 'net'
        self.net.mkdir(mode=0o700)
        self.directory = self.root / 'etc'
        self.directory.mkdir(mode=0o700)
        self.config = self.directory / 'wifi-hotspot.conf'

    def wifi(self, *names):
        for name in names:
            (self.net / name / 'wireless').mkdir(parents=True)

    def write(self, text, mode=0o600):
        self.config.write_bytes(text.encode() if isinstance(text, str) else text)
        self.config.chmod(mode)

    def run(self, *arguments, ok=True):
        result = subprocess.run([str(BINARY), *map(str, arguments)],
                                capture_output=True, text=True, timeout=10)
        if ok and result.returncode:
            raise AssertionError(result.stderr)
        if not ok and not result.returncode:
            raise AssertionError('Unexpected command success')
        return result

    def init(self, *arguments, ok=True):
        return self.run('--test-init-config', self.net, self.config, *arguments, ok=ok)

    def repair(self, *arguments, ok=True):
        return self.run('--test-repair-config-interface', self.net, self.config, *arguments, ok=ok)


class ConfigurationInitializationTests(unittest.TestCase):
    def test_explicit_interface_is_used_for_a_fresh_configuration(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-init-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            result = bench.init('--wifi-interface', 'wlp4s0', '--ssid', 'Desk Hotspot')
            self.assertNotIn('PASSPHRASE', result.stdout + result.stderr)
            text = bench.config.read_text()
            self.assertIn('WIFI_IFACE=wlp4s0\n', text)
            self.assertIn('INTERNET_IFACE=wlp4s0\n', text)
            self.assertIn('SSID=Desk Hotspot\n', text)
            password = re.search(r'^PASSPHRASE=(\S+)$', text, re.MULTILINE)[1]
            self.assertRegex(password, r'^[0-9a-f]{32}$')
            self.assertNotIn(password, result.stdout + result.stderr)
            self.assertEqual(bench.config.stat().st_mode & 0o777, 0o600)
            original = bench.config.read_bytes()
            bench.init('--wifi-interface', 'other0', '--ssid', 'Replacement')
            self.assertEqual(bench.config.read_bytes(), original)

    def test_detection_adopts_only_the_single_wifi_adapter(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-init-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            bench.init()
            self.assertIn('WIFI_IFACE=wlan0\n', bench.config.read_text())
            bench.config.unlink()
            bench.init('--detect-interface')
            self.assertIn('WIFI_IFACE=wlp4s0\n', bench.config.read_text())
            self.assertIn('INTERNET_IFACE=wlp4s0\n', bench.config.read_text())
        with tempfile.TemporaryDirectory(prefix='relayforge-init-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0', 'wlp5s0')
            bench.init('--detect-interface')
            self.assertIn('WIFI_IFACE=wlan0\n', bench.config.read_text())

    def test_backend_selection_and_option_errors(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-init-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            bench.init('--wifi-interface', 'wlp4s0', '--backend', 'networkmanager')
            self.assertIn('BACKEND=networkmanager\n', bench.config.read_text())
        with tempfile.TemporaryDirectory(prefix='relayforge-init-') as directory:
            bench = Bench(directory)
            bench.init('--backend', 'unsupported', ok=False)
            self.assertFalse(bench.config.exists())
            bench.init('--unknown', ok=False)
            bench.init('--ssid', ok=False)
            self.assertFalse(bench.config.exists())


class InterfaceRepairTests(unittest.TestCase):
    def test_repair_replaces_only_the_obsolete_default(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-repair-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            bench.write('WIFI_IFACE=INTERNET_IFACE=wlan0\nINTERNET_IFACE=wlan0\n'
                        'SSID=Mine\nPASSPHRASE=private-password\n')
            bench.repair()
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlp4s0\nINTERNET_IFACE=wlp4s0\n'
                             'SSID=Mine\nPASSPHRASE=private-password\n')
            self.assertEqual(bench.config.stat().st_mode & 0o777, 0o600)
            repaired = bench.config.read_bytes()
            second = bench.repair()
            self.assertIn('left unchanged', second.stderr)
            self.assertEqual(bench.config.read_bytes(), repaired)

    def test_repair_keeps_comments_line_endings_and_unrelated_values(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-repair-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            bench.write('# keep this comment\r\nWIFI_IFACE=wlan0\r\n'
                        'INTERNET_IFACE=eth0\r\nGATEWAY=192.168.50.1\r\n'
                        'PASSPHRASE="literal # password"\r\n')
            bench.repair()
            self.assertEqual(bench.config.read_bytes(),
                             b'# keep this comment\r\nWIFI_IFACE=wlp4s0\r\n'
                             b'INTERNET_IFACE=eth0\r\nGATEWAY=192.168.50.1\r\n'
                             b'PASSPHRASE="literal # password"\r\n')

    def test_repair_requires_absent_legacy_default_and_unambiguous_hardware(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-repair-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            bench.write('WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            (bench.net / 'wlan0').mkdir()
            bench.repair()
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            (bench.net / 'wlan0').rmdir()
            bench.wifi('wlp5s0')
            bench.repair()
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            bench.repair('--wifi-interface', 'wlp5s0')
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlp5s0\nINTERNET_IFACE=wlp5s0\n')

    def test_repair_refuses_unsafe_files_and_invalid_names(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-repair-') as directory:
            bench = Bench(directory)
            bench.wifi('wlp4s0')
            target = bench.root / 'real-config'
            target.write_text('WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            target.chmod(0o600)
            bench.config.symlink_to(target)
            bench.repair(ok=False)
            self.assertEqual(target.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            bench.config.unlink()
            bench.write('WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n', mode=0o644)
            bench.repair(ok=False)
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            bench.config.chmod(0o600)
            bench.repair('--wifi-interface', '-bad name', ok=False)
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            bench.repair('--wifi-interface', 'w' * 16, ok=False)
            self.assertEqual(bench.config.read_text(),
                             'WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n')
            bench.repair('--unknown', ok=False)


if __name__ == '__main__':
    unittest.main()
