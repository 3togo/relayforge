"""Desktop-independent client for NetworkManager's public D-Bus lifecycle API.

The service owns this connection, not a GUI. Volatile profiles and client-bound
activation let NetworkManager clean up when the worker disappears. The downstream
AP+STA capability is detected separately; it is not part of the public API.
"""
import xml.etree.ElementTree as ET

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

NM_NAME = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NATIVE_RELAY_CAPABILITY = 0x7001


class NetworkManager:
    def __init__(self):
        # A private bus connection lets bind-activation clean up on process exit.
        self.bus = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self._active = None
        self._user_disconnected = False
        # Subscribe before activation so a quick GUI Disconnect cannot be lost.
        self._state_subscription = self.bus.signal_subscribe(
            NM_NAME, NM_NAME + ".Connection.Active", "StateChanged", None, None,
            Gio.DBusSignalFlags.NONE, self._active_state_changed)

    def _active_state_changed(self, bus, sender, path, interface, signal, parameters):
        state, reason = parameters.unpack()
        if path == self._active and state in (3, 4) and reason == 2:
            # NM_ACTIVE_CONNECTION_STATE_REASON_USER_DISCONNECTED
            self._user_disconnected = True

    def user_disconnected(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        return self._user_disconnected

    def call(self, path, interface, method, parameters=None):
        return self.bus.call_sync(NM_NAME, path, interface, method, parameters,
                                  None, Gio.DBusCallFlags.NONE, 15000, None).unpack()

    def device(self, iface):
        return self.call(NM_PATH, NM_NAME, "GetDeviceByIpIface",
                         GLib.Variant("(s)", (iface,)))[0]

    def activate(self, device, profile):
        self._user_disconnected = False
        self._active = self.call(NM_PATH, NM_NAME, "AddAndActivateConnection2", GLib.Variant(
            "(a{sa{sv}}ooa{sv})", (profile, device, "/", {
                "persist": GLib.Variant("s", "volatile"),
                "bind-activation": GLib.Variant("s", "dbus-client"),
            })))[1]
        return self._active

    def state(self, active):
        return self.call(active, "org.freedesktop.DBus.Properties", "Get", GLib.Variant(
            "(ss)", (NM_NAME + ".Connection.Active", "State")))[0]

    def device_state(self, device):
        return self.call(device, "org.freedesktop.DBus.Properties", "Get", GLib.Variant(
            "(ss)", (NM_NAME + ".Device", "StateReason")))[0]

    def compatibility(self):
        xml = self.call(NM_PATH, "org.freedesktop.DBus.Introspectable", "Introspect")[0]
        tree = ET.fromstring(xml)
        method = tree.find(f"./interface[@name='{NM_NAME}']/method[@name='AddAndActivateConnection2']")
        inputs = [] if method is None else [a.get("type") for a in method.findall("arg")
                                           if a.get("direction", "in") == "in"]
        version = self.call(NM_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant(
            "(ss)", (NM_NAME, "Version")))[0]
        capabilities = self.call(NM_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant(
            "(ss)", (NM_NAME, "Capabilities")))[0]
        return {"native_wifi_relay": NATIVE_RELAY_CAPABILITY in capabilities,
                "daemon_version": version,
                "add_and_activate_connection2": inputs == ["a{sa{sv}}", "o", "o", "a{sv}"],
                "activation_options": "require-live-verification"}

    def deactivate(self, active):
        try:
            self.call(NM_PATH, NM_NAME, "DeactivateConnection", GLib.Variant("(o)", (active,)))
        except GLib.Error as exc:
            if Gio.DBusError.get_remote_error(exc) != NM_NAME + ".ConnectionNotActive":
                raise

    def close(self):
        self.bus.signal_unsubscribe(self._state_subscription)
        self.bus.close_sync(None)
