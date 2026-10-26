#!/usr/bin/env python3
"""Verify installed menu status; optional live cycle requires no connected clients."""
import argparse
import json
import time
from gi.repository import Gio, GLib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--open-settings', action='store_true')
parser.add_argument('--cycle', action='store_true', help='Stop and restart an active, client-free hotspot')
args = parser.parse_args()
session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
service = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
    'io.github.erhanzeyrek.WifiHotspot', '/io/github/erhanzeyrek/WifiHotspot',
    'io.github.erhanzeyrek.WifiHotspot', None)

def status():
    return json.loads(service.call_sync('GetStatus', None, 0, 5000, None).unpack()[0])

def menu_call(method, signature, values):
    return session.call_sync('org.freedesktop.network-manager-applet',
        '/org/ayatana/NotificationItem/nm_applet/Menu', 'com.canonical.dbusmenu',
        method, GLib.Variant(signature, values), None, 0, 5000, None).unpack()

def find_relay(node):
    if node[1].get('label') == 'Wi-Fi Relay':
        return node
    for child in node[2]:
        found = find_relay(child)
        if found:
            return found

def relay_menu():
    node = find_relay(menu_call('GetLayout', '(iias)', (0, -1, []))[1])
    assert node, 'Wi-Fi Relay submenu missing'
    return node

def click(item):
    menu_call('Event', '(isvu)', (item[0], 'clicked', GLib.Variant('i', 0), 0))

def verify(wanted=None):
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        try:
            current = status()
        except GLib.Error as error:
            # The service completes Start/Stop synchronously. A status request
            # can time out while that operation is still in progress.
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
                raise
            continue
        node = relay_menu()
        toggle, summary = node[2][:2]
        requested = current["active"] or current.get("desired_active", False)
        label = "Active" if current["active"] else "Waiting for Wi-Fi" if requested else "Off"
        expected = f"{label} · {current['client_count']} connected"
        if ((wanted is None or current['active'] == wanted)
                and toggle[1].get('toggle-state', 0) == int(requested)
                and toggle[1].get('enabled', True)
                and summary[1].get('label') == expected):
            return current, node
        time.sleep(1)
    raise AssertionError('Menu did not converge to service status')

before, node = verify()
if args.cycle:
    assert before['active'], 'Cycle requires an already active hotspot'
    assert before['client_count'] == 0, 'Refusing to disconnect connected clients'
    click(node[2][0])
    try:
        off, node = verify(False)
    finally:
        if not status()['active']:
            click(relay_menu()[2][0])
            verify(True)
    before, node = verify(True)
if args.open_settings:
    click(next(item for item in node[2] if item[1].get('label') == 'Open Wi-Fi Relay Settings…'))
print(json.dumps({'menu_present': True, 'status_matches_service': True,
    'active': before['active'], 'client_count': before['client_count'],
    'settings_action_sent': args.open_settings, 'live_cycle_verified': args.cycle}))
