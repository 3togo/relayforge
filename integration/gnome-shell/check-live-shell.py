#!/usr/bin/env python3
"""Regression check in an isolated GNOME Shell with test-only Eval enabled.

Set DBUS_SESSION_BUS_ADDRESS to that Shell's private session bus. Never enable
unsafe Eval in the user's normal session. The service action handler is replaced
only during the synchronous assertion, so this check cannot toggle the hotspot.
"""
import json
from gi.repository import Gio, GLib

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
script = r'''(() => {
    const extension = Main.extensionManager.lookup('wifi-relay@3togo.github.io');
    if (extension.state !== 1) throw new Error('Extension is not active');
    const e = extension.stateObj;
    const toggle = e._indicator._toggle;
    const original = toggle._onToggleClicked;
    let calls = 0;
    toggle._onToggleClicked = () => calls++;
    try {
        for (const status of [
            {active: false}, {active: true}, {active: true},
            {active: false, desired_active: true, state: 'connecting'},
            {active: false},
        ]) e.tray.update(status, [], false);
        if (calls !== 0) throw new Error(`Status refresh invoked ${calls} actions`);
        e.tray._switch.setToggleState(true);
        if (calls !== 1) throw new Error('User toggle no longer invokes an action');
        e.tray.update(toggle._status, toggle._clients, toggle._isBusy);
        if (calls !== 1) throw new Error('Restoring status invoked an action');
        return {status_updates_without_actions: true, user_toggle_action: true};
    } finally {
        toggle._onToggleClicked = original;
    }
})()'''
ok, result = bus.call_sync('org.gnome.Shell', '/org/gnome/Shell',
    'org.gnome.Shell', 'Eval', GLib.Variant('(s)', (script,)), None,
    Gio.DBusCallFlags.NONE, 10000, None).unpack()
assert ok, result
print(json.dumps(json.loads(result), indent=2))
