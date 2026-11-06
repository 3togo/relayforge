"""Safety gates for the opt-in destructive OS qualification harness."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('system_guest', ROOT/'tools/system-validation/guest.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class GuestGuardTests(unittest.TestCase):
    def test_stale_guest_packages_are_rejected_before_package_operations(self):
        with tempfile.TemporaryDirectory() as d:
            packages = Path(d) / 'packages'
            packages.mkdir()
            (packages / 'candidate.deb').write_bytes(b'old retained package')
            manifest = Path(d) / 'manifest.json'
            manifest.write_text(json.dumps({'candidate.deb': 'different-candidate-hash'}))
            with patch.object(guest, 'require_guest'), patch.object(guest, 'run') as run, \
                    patch('sys.argv', ['guest.py', '--packages', str(packages),
                                       '--manifest', str(manifest)]):
                with self.assertRaisesRegex(RuntimeError, 'package hashes'):
                    guest.main()
                run.assert_not_called()

    def test_ordinary_user_cannot_enter_package_checks(self):
        with patch.object(guest.os, 'geteuid', return_value=1000), patch.object(guest, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'guest root'):
                guest.main()
            run.assert_not_called()

    def test_unmarked_host_never_executes_commands(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d)/'missing'
            with patch.object(guest.os, 'geteuid', return_value=0), \
                    patch.object(guest, 'Path', return_value=marker), patch.object(guest, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'outside the marked disposable guest'):
                    guest.main()
                run.assert_not_called()

    def test_marker_on_non_vm_cannot_enter_package_checks(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d)/'marker'
            marker.write_text('disposable-task18')
            with patch.object(guest.os, 'geteuid', return_value=0), \
                    patch.object(guest, 'Path', return_value=marker), \
                    patch.object(guest, 'run', return_value=Mock(stdout='none\n')) as run:
                with self.assertRaisesRegex(RuntimeError, 'QEMU/KVM guest'):
                    guest.main()
                run.assert_called_once_with('systemd-detect-virt')

    def test_radio_passthrough_cannot_enter_package_checks(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d)/'marker'; marker.write_text('disposable-task18')
            radios = Path(d)/'radios'; radios.mkdir(); (radios/'phy0').mkdir()
            with patch.object(guest.os, 'geteuid', return_value=0), \
                    patch.object(guest, 'Path', side_effect=[marker, radios]), \
                    patch.object(guest, 'run', return_value=Mock(stdout='kvm\n')) as run:
                with self.assertRaisesRegex(RuntimeError, 'no Wi-Fi radios'):
                    guest.main()
                run.assert_called_once_with('systemd-detect-virt')


if __name__ == '__main__':
    unittest.main()
