"""Real Rust service and unmodified desktop client on a disposable private bus.

Run: dbus-run-session -- /usr/bin/python3 -m unittest discover -s tests -v
The service's --demo mode cannot launch the privileged networking worker.
"""
import ast
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']
sys.path.insert(0, str(ROOT / 'vendor/wifi-relay/settings'))
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib
from service_client import ServiceClient, decode_reply, BUS_NAME, OBJECT_PATH

BINARY = Path(os.environ.get('RELAYFORGE_BINARY', ROOT / 'target/debug/relayforge-service'))


def connect():
    return Gio.DBusConnection.new_for_address_sync(
        os.environ['DBUS_SESSION_BUS_ADDRESS'],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


def call(bus, method, params=None, interface=BUS_NAME):
    return bus.call_sync(BUS_NAME, OBJECT_PATH, interface, method, params, None,
                         Gio.DBusCallFlags.NONE, 5000, None)


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for private-bus result')


@contextlib.contextmanager
def running(mode='--demo', *arguments):
    bus = connect()
    with subprocess.Popen([str(BINARY), mode, *map(str, arguments)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            last_error = []
            def ready():
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode())
                try:
                    call(bus, 'GetStatus')
                    return True
                except GLib.Error as error:
                    last_error[:] = [str(error)]
                    return False
            try:
                wait_for(ready)
            except AssertionError as error:
                process.terminate()
                _, stderr = process.communicate(timeout=5)
                raise AssertionError(f'{error}: {last_error}; {stderr.decode()}') from error
            yield bus, process
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            bus.close_sync(None)


@contextlib.contextmanager
def fake_polkit(allowed=True):
    bus = connect()
    xml = '''<node><interface name="org.freedesktop.PolicyKit1.Authority">
      <method name="CheckAuthorization">
        <arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/>
        <arg type="a{ss}" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
        <arg type="(bba{ss})" direction="out"/>
      </method></interface></node>'''
    observed = []
    def check(_bus, _sender, _path, _iface, _method, params, invocation):
        observed.append(params.unpack())
        invocation.return_value(GLib.Variant('((bba{ss}))', ((allowed, False, {}),)))
    registration = bus.register_object('/org/freedesktop/PolicyKit1/Authority',
                                       Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], check, None, None)
    bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                  'RequestName', GLib.Variant('(su)', ('org.freedesktop.PolicyKit1', 4)),
                  None, Gio.DBusCallFlags.NONE, 5000, None)
    loop = GLib.MainLoop()
    thread = threading.Thread(target=loop.run)
    thread.start()
    try:
        yield observed
    finally:
        loop.quit()
        thread.join(5)
        bus.unregister_object(registration)
        bus.close_sync(None)


class CompatibilityTests(unittest.TestCase):
    def test_native_config_survives_restart_and_legacy_reader_sees_exact_values(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-config-') as directory:
            path = Path(directory) / 'hotspot.conf'
            created = subprocess.run([str(BINARY), '--init-config', str(path), '--ssid', 'Persistent',
                                      '--wifi-interface', 'wlp2s0'], capture_output=True, timeout=5)
            self.assertEqual(created.returncode, 0, created.stderr)
            original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with running('--demo-config', path) as (bus, _):
                before = decode_reply('GetConfig', call(bus, 'GetConfig'))
                self.assertEqual(len(before['PASSPHRASE']), 32)
                self.assertEqual(before['WIFI_IFACE'], 'wlp2s0')
                patch = {'SSID': 'Changed SSID', 'PASSPHRASE': "literal =#'\\$() spaces "}
                call(bus, 'SetConfig', GLib.Variant('(s)', (json.dumps(patch),)))
                saved = decode_reply('GetConfig', call(bus, 'GetConfig'))
                self.assertEqual(saved['PASSPHRASE'], patch['PASSPHRASE'])
                self.assertNotEqual(path.read_bytes(), original)
                # The pinned engine uses split-on-first '=' and literal values.
                values = dict(line.split('=', 1) for line in path.read_text().splitlines()
                              if line and not line.startswith('#') and '=' in line)
                self.assertEqual(values, saved)
            with running('--demo-config', path) as (bus, _):
                self.assertEqual(decode_reply('GetConfig', call(bus, 'GetConfig')), saved)
            retained = path.read_bytes()
            repeated = subprocess.run([str(BINARY), '--init-config', str(path)], capture_output=True, timeout=5)
            self.assertEqual(repeated.returncode, 0)
            self.assertEqual(path.read_bytes(), retained)
            self.assertNotIn(saved['PASSPHRASE'].encode(), created.stdout + created.stderr + repeated.stdout + repeated.stderr)

    def test_native_validation_checks_merged_psk_and_preserves_failed_save(self):
        with running() as (bus, _):
            original = decode_reply('GetConfig', call(bus, 'GetConfig'))
            for patch in [{'USE_PSK': '1'}, {'PASSPHRASE': 'a' * 64},
                          {'SSID': 'invisible\u200b'}, {'SSID': 'nonbreaking\u00a0space'}]:
                with self.assertRaises(GLib.Error):
                    call(bus, 'SetConfig', GLib.Variant('(s)', (json.dumps(patch),)))
                self.assertEqual(decode_reply('GetConfig', call(bus, 'GetConfig')), original)
            patch = {'USE_PSK': '1', 'PASSPHRASE': 'a' * 64}
            call(bus, 'SetConfig', GLib.Variant('(s)', (json.dumps(patch),)))
            call(bus, 'SetConfig', GLib.Variant('(s)', ('{"SSID":"PSK unchanged"}',)))
            saved = decode_reply('GetConfig', call(bus, 'GetConfig'))
            self.assertEqual(saved['PASSPHRASE'], 'a' * 64)
            self.assertEqual(saved['USE_PSK'], '1')

    def test_configuration_edits_wait_for_startup_and_survive_cancellation(self):
        with running() as (bus, _):
            results, errors = {}, []
            def request(key, method, params=None):
                connection = connect()
                try:
                    results[key] = decode_reply(method, call(connection, method, params))
                except Exception as error:
                    errors.append(error)
                finally:
                    connection.close_sync(None)
            start = threading.Thread(target=request, args=('start', 'Start'))
            start.start()
            wait_for(lambda: decode_reply('GetStatus', call(bus, 'GetStatus'))['state'] == 'connecting')
            edit = threading.Thread(target=request, args=('save', 'SetConfig',
                                    GLib.Variant('(s)', ('{"SSID":"After cancellation"}',))))
            edit.start()
            self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))
            start.join(5)
            edit.join(5)
            self.assertFalse(start.is_alive())
            self.assertFalse(edit.is_alive())
            self.assertFalse(errors)
            self.assertFalse(results['start']['success'])
            self.assertTrue(results['save'])
            self.assertEqual(decode_reply('GetConfig', call(bus, 'GetConfig'))['SSID'], 'After cancellation')
            self.assertEqual(decode_reply('GetStatus', call(bus, 'GetStatus'))['state'], 'idle')

    def test_malformed_external_configuration_does_not_break_status_or_echo_secrets(self):
        with tempfile.TemporaryDirectory(prefix='relayforge-config-') as directory:
            path = Path(directory) / 'hotspot.conf'
            subprocess.run([str(BINARY), '--init-config', str(path)], check=True, capture_output=True, timeout=5)
            with running('--demo-config', path) as (bus, _):
                path.write_text('PASSPHRASE=do-not-echo-secret\nUNKNOWN=bad\n')
                with self.assertRaises(GLib.Error) as error:
                    call(bus, 'GetConfig')
                self.assertNotIn('do-not-echo-secret', str(error.exception))
                self.assertFalse(decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])
                decode_reply('GetInterfaces', call(bus, 'GetInterfaces'))
                path.write_text('SSID=Incomplete\n')
                with self.assertRaises(GLib.Error):
                    call(bus, 'Start')
                self.assertFalse(decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])

    def test_backend_switch_is_blocked_until_stopped_but_ordinary_edits_are_retained(self):
        with running() as (bus, _):
            self.assertTrue(decode_reply('Start', call(bus, 'Start'))['success'])
            call(bus, 'SetConfig', GLib.Variant('(s)', ('{"SSID":"Next startup"}',)))
            with self.assertRaises(GLib.Error):
                call(bus, 'SetConfig', GLib.Variant('(s)', ('{"BACKEND":"networkmanager"}',)))
            self.assertEqual(decode_reply('GetConfig', call(bus, 'GetConfig'))['BACKEND'], 'create_ap')
            self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))
            call(bus, 'SetConfig', GLib.Variant('(s)', ('{"BACKEND":"networkmanager"}',)))
            self.assertEqual(decode_reply('GetConfig', call(bus, 'GetConfig'))['BACKEND'], 'networkmanager')

    def test_polkit_receives_actual_caller_identity(self):
        with fake_polkit() as observed, running('--demo-polkit') as (bus, _):
            decode_reply('GetConfig', call(bus, 'GetConfig'))
            self.assertEqual(observed[0][0], ('system-bus-name', {'name': bus.get_unique_name()}))
            self.assertEqual(observed[0][1], BUS_NAME + '.manage')
            self.assertEqual(observed[0][3], 1)

    def test_polkit_denial_and_unavailable_authority_fail_closed(self):
        with fake_polkit(False), running('--demo-polkit') as (bus, _):
            with self.assertRaises(GLib.Error) as error:
                call(bus, 'Start')
            self.assertIn('AccessDenied', str(error.exception))
        with running('--demo-polkit') as (bus, _):
            with self.assertRaises(GLib.Error) as error:
                call(bus, 'GetConfig')
            self.assertIn('AccessDenied', str(error.exception))

    def test_introspection_matches_existing_controls(self):
        tree = ast.parse((ROOT / 'vendor/wifi-relay/daemon/wifi-hotspot-daemon.py').read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'INTROSPECTION_XML'
                                  for target in node.targets))
        expected = eval(compile(ast.Expression(assignment.value), '<pinned XML>', 'eval'),
                        {'INTERFACE_NAME': BUS_NAME})
        def signatures(xml):
            interface = ET.fromstring(xml).find(f"interface[@name='{BUS_NAME}']")
            return {(child.tag, child.attrib['name']):
                    tuple((a.attrib.get('direction', 'out' if child.tag == 'signal' else 'in'),
                           a.attrib['type']) for a in child.findall('arg'))
                    for child in interface if child.tag in {'method', 'signal'}}
        with running() as (bus, _):
            actual = call(bus, 'Introspect', interface='org.freedesktop.DBus.Introspectable').unpack()[0]
            self.assertEqual(signatures(actual), signatures(expected))

    def test_unchanged_desktop_decoder_accepts_all_replies(self):
        with running() as (bus, _):
            for method in ['GetStatus', 'GetClients', 'GetConfig', 'GetInterfaces', 'GetCapabilities']:
                decode_reply(method, call(bus, method))
            self.assertTrue(decode_reply('SetConfig', call(bus, 'SetConfig', GLib.Variant('(s)', ('{"SSID":"New Demo"}',)))))
            start = decode_reply('Start', call(bus, 'Start'))
            self.assertTrue(start['success'])
            self.assertEqual(start['status']['ssid'], 'New Demo')
            self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))
            self.assertTrue(decode_reply('PrepareFirewall', call(bus, 'PrepareFirewall')))
            self.assertFalse(decode_reply('SwitchBandAndReconnect', call(bus, 'SwitchBandAndReconnect', GLib.Variant('(s)', ('5',))))['success'])

    def test_stop_cancels_start_and_status_stays_responsive(self):
        with running() as (bus, _):
            result = []
            errors = []
            def start():
                thread_bus = connect()
                try:
                    result.append(decode_reply('Start', call(thread_bus, 'Start')))
                except Exception as error:
                    errors.append(error)
                finally:
                    thread_bus.close_sync(None)
            thread = threading.Thread(target=start)
            thread.start()
            wait_for(lambda: decode_reply('GetStatus', call(bus, 'GetStatus'))['state'] == 'connecting')
            before = time.monotonic()
            self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))
            self.assertLess(time.monotonic() - before, .3)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertFalse(result[0]['success'])
            status = decode_reply('GetStatus', call(bus, 'GetStatus'))
            self.assertEqual(status['state'], 'idle')
            self.assertFalse(status['active'])
            self.assertFalse(status['desired_active'])

    def test_invalid_updates_do_not_change_configuration(self):
        with running() as (bus, _):
            original = decode_reply('GetConfig', call(bus, 'GetConfig'))
            for text in ['{"SSID":"x\\nBACKEND=evil"}', '{"SSID":"x","SSID":"y"}',
                         '{"WIFI_IFACE":"--help"}', '{"SSID":3}', '[]',
                         '{"CHANNEL":"999"}', '{"SSID":"' + 'x' * 65536 + '"}']:
                with self.assertRaises(GLib.Error):
                    call(bus, 'SetConfig', GLib.Variant('(s)', (text,)))
            self.assertEqual(original, decode_reply('GetConfig', call(bus, 'GetConfig')))

    def test_denied_methods_never_reach_backend(self):
        with running('--demo-deny') as (bus, _):
            protected = {'Start': None, 'Stop': None, 'GetConfig': None, 'PrepareFirewall': None,
                         'SetConfig': GLib.Variant('(s)', ('{}',)),
                         'SwitchBandAndReconnect': GLib.Variant('(s)', ('5',))}
            for method, params in protected.items():
                with self.assertRaises(GLib.Error) as error:
                    call(bus, method, params)
                self.assertIn('AccessDenied', str(error.exception))
            self.assertFalse(decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])
            decode_reply('GetClients', call(bus, 'GetClients'))

    def test_second_service_cannot_replace_first(self):
        with running() as (bus, first):
            second = subprocess.run([str(BINARY), '--demo'], capture_output=True, timeout=5)
            self.assertNotEqual(second.returncode, 0)
            self.assertIsNone(first.poll())
            decode_reply('GetStatus', call(bus, 'GetStatus'))

    def test_existing_async_client_receives_status_and_clients_signals(self):
        with running():
            ready, signals, replies = [], [], []
            client = ServiceClient(lambda error: ready.append(error), lambda name, data: signals.append((name, data)))
            try:
                wait_for(lambda: ready)
                self.assertIsNone(ready[0])
                client.call('Start', lambda value, error: replies.append((value, error)))
                wait_for(lambda: replies)
                self.assertIsNone(replies[0][1])
                self.assertTrue(replies[0][0]['success'])
                client.call('Stop', lambda value, error: replies.append((value, error)))
                wait_for(lambda: len(replies) == 2 and any(name == 'ClientsChanged' for name, _ in signals))
                self.assertTrue(any(name == 'StatusChanged' and data['active'] for name, data in signals))
                self.assertTrue(any(name == 'StatusChanged' and data['state'] == 'idle' for name, data in signals))
            finally:
                client.close()


if __name__ == '__main__':
    unittest.main()
