"""Losing the Wi-Fi radio must stop owned sharing instead of reporting a stale AP.

The kill switch is announced by NetworkManager's WirelessEnabled property. The
service owns the AP, so it has to release it; leaving it running would keep an
"active" status for an interface that can no longer carry traffic.
"""
import contextlib
import json
import os
import select
import subprocess
import tempfile
import time
import unittest

import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

from test_dbus import ROOT, call, connect, decode_reply, running, wait_for
from test_networkmanager import Bench

TEST = 'io.github.relayforge.TestNetworkManager'
NM = 'org.freedesktop.NetworkManager'
NM_ROOT = '/org/freedesktop/NetworkManager'
PROPERTIES = 'org.freedesktop.DBus.Properties'


@contextlib.contextmanager
def bench(**options):
    with tempfile.TemporaryDirectory(prefix='relayforge-radio-') as directory:
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


class RadioWatchTests(unittest.TestCase):
    def radio(self, bus, enabled):
        bus.call_sync(NM, NM_ROOT, TEST, 'EmitRadio',
                      GLib.Variant('(s)', (json.dumps({'enabled': enabled}),)),
                      None, Gio.DBusCallFlags.NONE, 5000, None)

    def test_switching_the_radio_off_stops_owned_sharing_and_cleans_up(self):
        with bench(client=True) as context, context.service() as (bus, _):
            started = decode_reply('Start', call(bus, 'Start'))
            self.assertTrue(started['success'], started)
            wait_for(lambda: decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])
            self.radio(bus, False)
            wait_for(lambda: not decode_reply('GetStatus', call(bus, 'GetStatus'))['active'],
                     seconds=10)
            self.assertFalse(decode_reply('GetStatus', call(bus, 'GetStatus')).get('desired_active'))
            context.assert_clean(self)

    def test_enabling_the_radio_again_never_auto_starts_sharing(self):
        """A radio toggle is not a request to share; Start stays explicit."""
        with bench() as context, context.service() as (bus, _):
            self.radio(bus, True)
            time.sleep(0.5)
            status = decode_reply('GetStatus', call(bus, 'GetStatus'))
            self.assertFalse(status['active'])
            self.assertFalse(status.get('desired_active'))
            self.assertFalse(context.events('activation-request'))

    def test_a_radio_signal_from_a_foreign_sender_is_ignored(self):
        """Only NetworkManager may order a stop, never another desktop client."""
        with bench(client=True) as context, context.service() as (bus, _):
            started = decode_reply('Start', call(bus, 'Start'))
            self.assertTrue(started['success'], started)
            wait_for(lambda: decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])
            other = connect()
            try:
                other.emit_signal(None, NM_ROOT, PROPERTIES, 'PropertiesChanged',
                                  GLib.Variant('(sa{sv}as)',
                                               (NM, {'WirelessEnabled': GLib.Variant('b', False)}, [])))
            finally:
                other.close_sync(None)
            time.sleep(1)
            self.assertTrue(decode_reply('GetStatus', call(bus, 'GetStatus'))['active'])
            self.assertTrue(decode_reply('Stop', call(bus, 'Stop')))
            context.assert_clean(self)


if __name__ == '__main__':
    unittest.main()
