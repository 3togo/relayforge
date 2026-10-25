"""ExecStopPost recovery of journal-owned resources after an unclean stop.

The pinned systemd unit runs `nm_backend.py --recover-all` after every service
stop, including a crash, so a killed daemon never strands an owned profile,
interface or helper process. These tests drive the native recovery entry
against the private simulators: no networking and no system bus.
"""
import subprocess
from pathlib import Path
import unittest

from test_create_ap import bench as ap_bench
from test_dbus import BINARY, ROOT, call, decode_reply
from test_networkmanager import bench as nm_bench


def recover(*mode):
    return subprocess.run([str(BINARY), *map(str, mode), '--recover-all'],
                          capture_output=True, timeout=30, text=True)


class CreateApTeardownTests(unittest.TestCase):
    def test_crashed_ownership_is_released_without_the_live_daemon(self):
        with ap_bench() as context:
            with context.service() as (bus, process):
                self.assertTrue(decode_reply('Start', call(bus, 'Start'))['success'])
                pid = context.record()['process']['pid']
                process.kill()
                process.wait(5)
            self.assertTrue((context.state / 'create-ap.json').exists())
            self.assertTrue(Path(f'/proc/{pid}').exists())
            result = recover('--test-create-ap', context.radio, context.config,
                             context.state, ROOT / 'tests/fake_create_ap.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
            context.clean(self)
            self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_recovery_is_idempotent_and_needs_no_configuration(self):
        with ap_bench() as context:
            arguments = ('--test-create-ap', context.radio, context.config,
                         context.state, ROOT / 'tests/fake_create_ap.sh')
            for _ in range(2):
                result = recover(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
            context.clean(self)
            # Teardown must also run when the configuration is gone or broken:
            # ExecStopPost fires on every stop, including a failed startup.
            result = recover('--test-create-ap', context.radio,
                             context.root / 'missing', context.state,
                             ROOT / 'tests/fake_create_ap.sh')
            self.assertEqual(result.returncode, 0, result.stderr)


class NetworkManagerTeardownTests(unittest.TestCase):
    def test_crashed_ownership_is_released_without_the_live_daemon(self):
        for native in (False, True):
            with self.subTest(native=native), nm_bench(native=native) as (context, _):
                with context.service() as (bus, process):
                    self.assertTrue(decode_reply('Start', call(bus, 'Start'))['success'])
                    process.kill()
                    process.wait(5)
                self.assertTrue((context.state / 'networkmanager.json').exists())
                result = recover('--test-networkmanager', context.path / 'capture.json',
                                 context.config, context.state)
                self.assertEqual(result.returncode, 0, result.stderr)
                context.assert_clean(self)


class OptionContractTests(unittest.TestCase):
    def test_recover_all_requires_a_native_mode(self):
        result = recover('--demo')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--recover-all', result.stderr)


if __name__ == '__main__':
    unittest.main()
