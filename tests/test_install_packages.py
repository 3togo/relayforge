"""Boot policy survives historical hooks and failed APT transactions."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_packages', ROOT / 'tools/install-packages.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallPackagesTests(unittest.TestCase):
    def transaction(self, initial, apt_code=0, restore_fails=False,
                    apt_state='enabled', target_unit='relayforge-native.service'):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            package = work / 'old.deb'
            package.write_bytes(b'fixture archive')
            receipt = work / 'receipt.json'
            states = dict(zip(installer.UNITS, (initial, 'not-found')))
            calls = []

            def command(*args):
                calls.append(args)
                code, output = 0, ''
                if args[:2] == ('dpkg-deb', '--contents'):
                    output = '-rw-r--r-- root/root 100 2026-11-12 00:00 ./usr/lib/systemd/system/' + target_unit + '\n'
                elif args[0] == 'dpkg-deb':
                    output = 'relayforge\n'
                elif args[:2] == ('systemctl', 'is-enabled'):
                    output = states[args[2]]
                    code = {'enabled': 0, 'disabled': 1, 'not-found': 4, 'masked': 1}[output]
                elif args[0] == 'apt-get':
                    # Match the actual 0.1.2 failure: its postinst enables a
                    # default-disabled daemon, including before hook failure.
                    states[installer.UNITS[0]] = apt_state
                    code = apt_code
                elif args[0] == 'systemctl' and args[1] in ('enable', 'disable'):
                    code = 1 if restore_fails else 0
                    if not code:
                        states[args[2]] = 'enabled' if args[1] == 'enable' else 'disabled'
                else:
                    self.fail(f'Unexpected side effect: {args}')
                return subprocess.CompletedProcess(args, code, output)

            with patch.object(installer.os, 'geteuid', return_value=0), patch.object(installer, 'run', side_effect=command):
                if target_unit != installer.UNITS[0]:
                    with self.assertRaisesRegex(ValueError, 'backend differs'):
                        installer.install([package], receipt)
                elif initial == 'masked' or apt_code or restore_fails:
                    with self.assertRaises(RuntimeError):
                        installer.install([package], receipt)
                else:
                    installer.install([package], receipt)
            return (json.loads(receipt.read_text()) if receipt.exists() else None,
                    states, calls)

    def test_historical_hook_enable_is_reversed_without_starting_or_stopping(self):
        report, states, calls = self.transaction('disabled')
        self.assertTrue(report['complete'])
        self.assertEqual(states[installer.UNITS[0]], 'disabled')
        self.assertIn(('systemctl', 'disable', installer.UNITS[0]), calls)
        self.assertFalse(any(set(call) & {'start', 'stop', 'restart'} for call in calls))

    def test_enabled_policy_remains_enabled(self):
        report, states, calls = self.transaction('enabled')
        self.assertTrue(report['complete'])
        self.assertEqual(states[installer.UNITS[0]], 'enabled')
        self.assertNotIn(('systemctl', 'disable', installer.UNITS[0]), calls)

    def test_enabled_policy_is_restored_when_package_disables_it(self):
        report, states, calls = self.transaction('enabled', apt_state='disabled')
        self.assertTrue(report['complete'])
        self.assertEqual(states[installer.UNITS[0]], 'enabled')
        self.assertIn(('systemctl', 'enable', installer.UNITS[0]), calls)

    def test_failed_apt_still_restores_policy_and_reports_failure(self):
        report, states, _ = self.transaction('disabled', apt_code=100)
        self.assertFalse(report['complete'])
        self.assertTrue(report['boot_policy_preserved'])
        self.assertEqual(report['apt_exit_code'], 100)
        self.assertEqual(states[installer.UNITS[0]], 'disabled')

    def test_failed_restoration_cannot_report_success(self):
        report, _, _ = self.transaction('disabled', restore_fails=True)
        self.assertFalse(report['complete'])
        self.assertFalse(report['boot_policy_preserved'])
        self.assertIn(installer.UNITS[0], report['restoration_errors'][0])

    def test_masked_units_are_rejected_before_apt(self):
        report, _, calls = self.transaction('masked')
        self.assertIsNone(report)
        self.assertFalse(any(call[0] == 'apt-get' for call in calls))

    def test_backend_change_is_rejected_before_apt(self):
        report, _, calls = self.transaction('disabled', target_unit=installer.UNITS[1])
        self.assertIsNone(report)
        self.assertFalse(any(call[0] == 'apt-get' for call in calls))
