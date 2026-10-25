"""Native upstream band switching on a disposable bus and simulated NetworkManager.

The desktop controls expose SwitchBandAndReconnect because a hotspot may only use
channels the PHY advertises without DFS/CAC. These tests drive the Rust
implementation against the simulated bus: no adapter, scan or real activation.
"""
import contextlib
import json
import select
import subprocess
import tempfile
import unittest

from test_dbus import ROOT, GLib, ServiceClient, call, decode_reply, running, wait_for
from test_networkmanager import Bench


def state(context):
    return json.loads((context.path / 'control.json').read_text())


def switch(bus, target):
    # A real scan can take seconds; the default 5 s call timeout is too tight here.
    reply = bus.call_sync('io.github.erhanzeyrek.WifiHotspot', '/io/github/erhanzeyrek/WifiHotspot',
                          'io.github.erhanzeyrek.WifiHotspot', 'SwitchBandAndReconnect',
                          GLib.Variant('(s)', (target,)), None, 0, 30000, None)
    return decode_reply('SwitchBandAndReconnect', reply)


@contextlib.contextmanager
def bench(**options):
    with tempfile.TemporaryDirectory(prefix='relayforge-band-') as directory:
        context = Bench(directory, options)
        with subprocess.Popen(['/usr/bin/python3', str(ROOT / 'tests/fake_networkmanager.py'), directory],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as fake:
            try:
                ready, _, _ = select.select([fake.stdout], [], [], 5)
                if not ready or fake.stdout.readline() != b'ready\n':
                    raise AssertionError('Fake NetworkManager did not become ready')
                yield context
            finally:
                fake.terminate()
                _, stderr = fake.communicate(timeout=5)
                if b'Traceback' in stderr:
                    raise AssertionError(stderr.decode())


class NativeBandSwitchTests(unittest.TestCase):
    def test_switch_moves_the_station_to_a_permitted_channel(self):
        for target, frequency in (('2.4', 2437), ('5', 5745)):
            with self.subTest(target=target), bench() as context:
                with context.service() as (bus, process):
                    reply = switch(bus, target)
                    self.assertTrue(reply['success'], reply)
                    self.assertEqual(reply['target_band'], target)
                    self.assertEqual(reply['ssid'], 'Test upstream')
                    self.assertEqual(bool(state(context).get('band5')), frequency == 5745)
                    self.assertTrue(context.events('scan-requested'))
                    self.assertEqual(len(context.events('band-activate')), 1)

    def test_a_failed_reconnect_reports_an_error_without_claiming_success(self):
        with bench(switch_fails=True) as context:
            with context.service() as (bus, process):
                reply = switch(bus, '2.4')
                self.assertFalse(reply['success'], reply)
                self.assertTrue(reply['error'])
                self.assertFalse(state(context).get('roamed'))

    def test_roaming_back_retries_with_a_volatile_constrained_profile(self):
        """NetworkManager may accept the BSSID and immediately pick a stronger AP."""
        with bench(roam_back=True) as context:
            with context.service() as (bus, process):
                reply = switch(bus, '5')
                self.assertTrue(reply['success'], reply)
                pinned = context.events('band-pin')
                self.assertEqual(len(pinned), 1)
                self.assertEqual(pinned[0]['band'], 'a')
                # The constrained activation must now show the wanted channel.
                self.assertTrue(state(context).get('band5'))

    def test_unsupported_band_is_rejected_before_touching_the_bus(self):
        with bench() as context:
            with context.service() as (bus, process):
                for target in ('3', '6 GHz'):
                    with self.assertRaises(GLib.Error):
                        switch(bus, target)
                self.assertFalse(context.events('scan-requested'))

    def test_switching_is_refused_while_sharing_is_active(self):
        with bench() as context:
            with context.service() as (bus, process):
                started = decode_reply('Start', call(bus, 'Start'))
                self.assertTrue(started['success'], started)
                reply = switch(bus, '2.4')
                self.assertFalse(reply['success'], reply)
                self.assertIn('Stop sharing', reply['error'])
                self.assertFalse(context.events('band-activate'))
                self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))

    def test_a_foreign_network_is_never_selected(self):
        """Only APs serving the current network on a permitted channel are candidates."""
        with bench(no_scan=True) as context:
            with context.service() as (bus, process):
                reply = switch(bus, '2.4')
                self.assertFalse(reply['success'], reply)
                self.assertIn('No reachable', reply['error'])
                self.assertFalse(context.events('band-activate'))

    def test_switch_broadcasts_the_connecting_transition_before_the_refresh(self):
        """The unchanged tray shows a reconnect immediately, not at the next poll."""
        with bench() as context:
            with context.service() as (bus, process):
                ready, signals = [], []
                client = ServiceClient(lambda error: ready.append(error),
                                       lambda name, data: signals.append((name, data)))
                try:
                    wait_for(lambda: ready)
                    reply = switch(bus, '2.4')
                    self.assertTrue(reply['success'], reply)
                    states = lambda: [data['state'] for name, data in signals
                                      if name == 'StatusChanged' and isinstance(data, dict)]
                    wait_for(lambda: states())
                    self.assertEqual(states()[0], 'connecting')
                finally:
                    client.close()


if __name__ == '__main__':
    unittest.main()
