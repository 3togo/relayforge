"""Read each Wi-Fi adapter driver's hotspot capability from NetworkManager."""
from gi.repository import Gio, GLib

# NMDeviceWifiCapabilities.NM_WIFI_DEVICE_CAP_AP, from nm-dbus-interface.h.
AP_CAPABILITY = 0x40


def explanation(name):
    return (name + ' cannot create a hotspot: its current driver does not support '
            'Access Point (AP) mode. Select another Wi-Fi adapter. '
            'This adapter can still be used as an Internet source.')


def restricted(capabilities):
    return not bool(capabilities & AP_CAPABILITY)


class AdapterLimit:
    def __init__(self, changed):
        self.changed = changed
        self.blocked = set()
        self.pending = False
        self.cancel = Gio.Cancellable()

    def close(self):
        self.cancel.cancel()

    def refresh(self, interfaces):
        if self.pending or self.cancel.is_cancelled():
            return
        names = set(interfaces)
        self.pending = True
        # Retain each adapter's last observation if its query fails. A failure
        # for one device must not prevent updates to the other devices.
        blocked = self.blocked & names
        remaining = len(names)

        def publish():
            self.pending = False
            if not self.cancel.is_cancelled() and blocked != self.blocked:
                self.blocked = blocked
                self.changed()

        def finish(name, capabilities=None):
            nonlocal remaining
            if capabilities is not None:
                if restricted(capabilities):
                    blocked.add(name)
                else:
                    blocked.discard(name)
            remaining -= 1
            if remaining == 0:
                publish()

        if not names:
            publish()
            return

        def ready(_source, result):
            try:
                bus = Gio.bus_get_finish(result)
            except GLib.Error:
                publish()
                return

            def call(name, path, interface, method, params, continuation):
                def reply(connection, result):
                    try:
                        value = connection.call_finish(result).unpack()[0]
                        while isinstance(value, GLib.Variant):
                            value = value.unpack()
                    except GLib.Error:
                        finish(name)
                        return
                    continuation(value)
                bus.call('org.freedesktop.NetworkManager', path, interface, method,
                         params, None, Gio.DBusCallFlags.NONE, 2000, self.cancel, reply)

            def device(name, path):
                call(name, path, 'org.freedesktop.DBus.Properties', 'Get',
                     GLib.Variant('(ss)', ('org.freedesktop.NetworkManager.Device.Wireless', 'WirelessCapabilities')),
                     lambda caps: finish(name, caps))

            for name in names:
                call(name, '/org/freedesktop/NetworkManager', 'org.freedesktop.NetworkManager',
                     'GetDeviceByIpIface', GLib.Variant('(s)', (name,)),
                     lambda path, name=name: device(name, path))

        Gio.bus_get(Gio.BusType.SYSTEM, self.cancel, ready)
