"""Bound VM cleanup without starting a VM or contacting an SSH server."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'system_runner', ROOT / 'tools/system-validation/run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_commands_have_a_deadline_and_allow_explicit_overrides(self):
        with patch.object(runner.subprocess, 'run') as run:
            runner.run(['qemu-img', 'info', 'image'])
            self.assertEqual(run.call_args.kwargs['timeout'], 120)
            self.assertTrue(run.call_args.kwargs['check'])
            runner.run(['scp', 'packages', 'guest:'], timeout=300)
            self.assertEqual(run.call_args.kwargs['timeout'], 300)

    def test_graceful_shutdown_reaps_without_sending_signals(self):
        vm = Mock()
        with patch.object(runner.subprocess, 'run') as poweroff:
            runner.stop_vm(vm, ['ssh', 'guest'])
        self.assertEqual(poweroff.call_args.args[0], ['ssh', 'guest', 'sudo poweroff'])
        self.assertEqual(poweroff.call_args.kwargs['timeout'], 10)
        vm.wait.assert_called_once_with(timeout=30)
        vm.terminate.assert_not_called()
        vm.kill.assert_not_called()

    def test_ssh_failure_cannot_skip_vm_cleanup(self):
        for error in (subprocess.TimeoutExpired('ssh', 10), OSError('ssh unavailable')):
            with self.subTest(error=error):
                vm = Mock()
                vm.wait.side_effect = [subprocess.TimeoutExpired('qemu', 30), 0]
                with patch.object(runner.subprocess, 'run', side_effect=error):
                    runner.stop_vm(vm, ['ssh', 'guest'])
                vm.terminate.assert_called_once_with()
                vm.kill.assert_not_called()
                self.assertEqual(vm.wait.call_count, 2)

    def test_unresponsive_vm_is_killed_and_reaped(self):
        vm = Mock()
        vm.wait.side_effect = [subprocess.TimeoutExpired('qemu', 30),
                               subprocess.TimeoutExpired('qemu', 10), 0]
        with patch.object(runner.subprocess, 'run'):
            runner.stop_vm(vm, ['ssh', 'guest'])
        vm.terminate.assert_called_once_with()
        vm.kill.assert_called_once_with()
        self.assertEqual(vm.wait.call_args_list[-1].kwargs, {'timeout': 10})

    def test_shutdown_interrupt_still_reaps_vm(self):
        vm = Mock()
        with patch.object(runner.subprocess, 'run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.stop_vm(vm, ['ssh', 'guest'])
        vm.wait.assert_called_once_with(timeout=30)


if __name__ == '__main__':
    unittest.main()
