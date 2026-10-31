import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from settings.preferences import get_boolean, set_boolean, read_preferences, MAX_PREFERENCE_BYTES
from settings.lifecycle import CodeRevision


class PreferenceTests(unittest.TestCase):
    def test_writes_are_private_and_preserve_unrelated_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.json'
            path.write_text('{"other":"keep"}')
            set_boolean(path, 'visible', False)
            self.assertFalse(get_boolean(path, 'visible'))
            self.assertEqual(read_preferences(path)['other'], 'keep')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_replace_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.json'
            path.write_text('{"visible":true}')
            with patch('settings.preferences.os.replace', side_effect=OSError('Disk error')):
                with self.assertRaises(OSError):
                    set_boolean(path, 'visible', False)
            self.assertTrue(get_boolean(path, 'visible'))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_oversized_or_nonobject_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.json'
            for contents in ['[]', 'x' * (MAX_PREFERENCE_BYTES + 1), '[' * 2000 + '0' + ']' * 2000]:
                path.write_text(contents)
                self.assertEqual(read_preferences(path), {})

    def test_startup_disk_failure_restores_extension_choices(self):
        from settings.startup import set_auto_start, UUID
        from unittest.mock import Mock
        state = {'enabled-extensions': ['other'], 'disabled-extensions': [UUID]}
        original = {key: list(value) for key, value in state.items()}
        settings = Mock()
        settings.get_strv.side_effect = lambda key: list(state[key])
        def save(key, values):
            state[key] = list(values)
            return True
        settings.set_strv.side_effect = save
        with patch('settings.startup.set_boolean', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                set_auto_start(True, settings, Mock())
        self.assertEqual(state, original)


class CodeRevisionTests(unittest.TestCase):
    def test_changed_module_is_detected_only_after_upgrade_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first.py', Path(directory) / 'second.py'
            first.write_text('old'); second.write_text('old')
            revision = CodeRevision([first, second])
            self.assertFalse(revision.changed())
            second.unlink(); first.write_text('new code')
            self.assertFalse(revision.changed())
            second.write_text('new')
            self.assertTrue(revision.changed())


class CodeRemovalTests(unittest.TestCase):
    def test_temporary_upgrade_gap_and_persistent_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tray.py'
            path.touch()
            revision = CodeRevision([path])
            path.unlink()
            self.assertFalse(revision.removed(now=100))
            self.assertFalse(revision.removed(now=105))
            path.touch()
            self.assertFalse(revision.removed(now=109))
            path.unlink()
            self.assertFalse(revision.removed(now=110))
            self.assertTrue(revision.removed(now=120))


class TrayReplacementTests(unittest.TestCase):
    def invoke(self, uid=1000, command=b'/usr/bin/python3\0/usr/share/wifi-hotspot/settings/tray.py\0', owner_changed=False, exited=True):
        from gi.repository import Gio, GLib
        from settings.lifecycle import replace_tray_instance
        from unittest.mock import Mock
        import contextlib
        bus = Mock()
        owners = iter([':1.123', ':1.124' if owner_changed else ':1.123'])
        def reply(_destination, _path, _interface, method, *_args):
            values = {'NameHasOwner': ('b', True), 'GetConnectionUnixUser': ('u', uid),
                      'GetConnectionUnixProcessID': ('u', 4321)}
            signature, value = ('s', next(owners)) if method == 'GetNameOwner' else values[method]
            return GLib.Variant('('+signature+')', (value,))
        bus.call_sync.side_effect = reply
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(Gio, 'bus_get_sync', return_value=bus))
            stack.enter_context(patch('os.geteuid', return_value=1000))
            stack.enter_context(patch('os.getpid', return_value=9999))
            stack.enter_context(patch('os.pidfd_open', return_value=99))
            stack.enter_context(patch('pathlib.Path.read_bytes', return_value=command))
            send = stack.enter_context(patch('signal.pidfd_send_signal'))
            close = stack.enter_context(patch('os.close'))
            stack.enter_context(patch('select.select', return_value=([99] if exited else [], [], [])))
            try:
                replace_tray_instance('io.github.erhanzeyrek.WifiHotspot.Tray', '/usr/share/wifi-hotspot/settings/tray.py')
            finally:
                self.send = send
                self.close = close
                self.bus = bus

    def test_only_matching_user_tray_is_retired_without_hotspot_call(self):
        import signal
        self.invoke()
        self.send.assert_called_once_with(99, signal.SIGTERM)
        self.close.assert_called_once_with(99)
        self.assertTrue(all(call.args[0] == 'org.freedesktop.DBus' for call in self.bus.call_sync.call_args_list))

    def test_another_user_or_another_application_is_never_signalled(self):
        with self.assertRaises(PermissionError):
            self.invoke(uid=1001)
        self.send.assert_not_called()
        with self.assertRaises(PermissionError):
            self.invoke(command=b'/usr/bin/python3\0/usr/bin/wifi-hotspot-settings\0')
        self.send.assert_not_called()
        self.close.assert_called_once_with(99)

    def test_owner_change_prevents_signalling_stale_instance(self):
        self.invoke(owner_changed=True)
        self.send.assert_not_called()

    def test_shutdown_has_a_bounded_wait(self):
        with self.assertRaises(TimeoutError):
            self.invoke(exited=False)
        self.close.assert_called_once_with(99)

    def test_root_replacement_is_refused(self):
        from settings.lifecycle import replace_tray_instance
        with patch('os.geteuid', return_value=0), self.assertRaises(PermissionError):
            replace_tray_instance('io.github.erhanzeyrek.WifiHotspot.Tray', '/usr/share/wifi-hotspot/settings/tray.py')
