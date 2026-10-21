"""Asynchronous desktop access to Relay, with bounded replies and owned requests."""
import json
from gi.repository import Gio, GLib

BUS_NAME = 'io.github.erhanzeyrek.WifiHotspot'
OBJECT_PATH = '/io/github/erhanzeyrek/WifiHotspot'
MAX_REPLY_BYTES = 1024 * 1024


def decode_reply(method, reply):
    values = reply.unpack()
    if len(values) != 1:
        raise ValueError('Invalid service reply')
    value = values[0]
    if method in {'Stop', 'SetConfig', 'PrepareFirewall'}:
        if type(value) is not bool:
            raise ValueError('Invalid service reply')
        return value
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_REPLY_BYTES:
        raise ValueError('Invalid service reply')
    try:
        value = json.loads(value)
    except RecursionError as error:
        raise ValueError('Invalid service reply nesting') from error
    if method == 'GetClients':
        if (not isinstance(value, list) or len(value) > 1024
                or any(not isinstance(client, dict) for client in value)
                or any(client.get(key) is not None and not isinstance(client[key], str)
                       for client in value for key in ('hostname', 'ip', 'mac'))):
            raise ValueError('Invalid client list')
        return value
    if not isinstance(value, dict):
        raise ValueError('Invalid service reply')
    if method == 'GetStatus':
        if (type(value.get('active')) is not bool
                or ('desired_active' in value and type(value['desired_active']) is not bool)
                or ('client_count' in value and (type(value['client_count']) is not int
                                                or value['client_count'] < 0))):
            raise ValueError('Invalid hotspot status')
        if any(value.get(key) is not None and not isinstance(value[key], str)
               for key in ('state', 'ssid', 'iface', 'phy_iface', 'backend', 'band', 'error')):
            raise ValueError('Invalid hotspot status')
    elif method == 'GetConfig':
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
            raise ValueError('Invalid service configuration')
    elif method == 'GetInterfaces':
        if any(not isinstance(value.get(key), list)
               or any(not isinstance(item, str) for item in value[key])
               for key in ('wifi_interfaces', 'all_interfaces')):
            raise ValueError('Invalid interface list')
    elif method in {'Start', 'SwitchBandAndReconnect'}:
        if (type(value.get('success')) is not bool
                or (value.get('error') is not None and not isinstance(value['error'], str))):
            raise ValueError('Invalid operation result')
        if value.get('status') is not None:
            decode_reply('GetStatus', GLib.Variant('(s)', (json.dumps(value['status']),)))
    return value


class ServiceClient:
    def __init__(self, ready, signal=None):
        self.proxy = None
        self.closed = False
        self.cancel = Gio.Cancellable()
        self.generation = 0
        self._ready = ready
        self._signal = signal
        self._handlers = []
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                                 None, BUS_NAME, OBJECT_PATH, BUS_NAME, self.cancel,
                                 self._proxy_ready)

    def _proxy_ready(self, _source, result):
        try:
            proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as error:
            if not self.closed:
                self._ready(error)
            return
        if self.closed:
            return
        self.proxy = proxy
        self._handlers = [proxy.connect('notify::g-name-owner', self._owner_changed),
                          proxy.connect('g-signal', self._on_signal)]
        self._ready(None)

    def _owner_changed(self, _proxy, _property):
        self.generation += 1
        if not self.closed:
            self._ready(None)

    def _on_signal(self, _proxy, _sender, name, parameters):
        if self.closed or not self._signal or name not in {'StatusChanged', 'ClientsChanged'}:
            return
        try:
            data = decode_reply('GetStatus' if name == 'StatusChanged' else 'GetClients', parameters)
        except (ValueError, TypeError, UnicodeError):
            return
        self._signal(name, data)

    def call(self, method, callback, parameters=None, timeout=10000):
        if self.closed:
            return
        if self.proxy is None:
            callback(None, RuntimeError('Hotspot service is unavailable'))
            return
        generation = self.generation
        def finished(proxy, result):
            data, error = None, None
            try:
                reply = proxy.call_finish(result)
                data = decode_reply(method, reply)
            except (GLib.Error, ValueError, TypeError, UnicodeError) as failure:
                error = failure
            if self.closed:
                return
            if generation != self.generation:
                data, error = None, RuntimeError('Hotspot service restarted; retry the request')
            callback(data, error)
        try:
            self.proxy.call(method, parameters, Gio.DBusCallFlags.NONE, timeout, self.cancel, finished)
        except GLib.Error as error:
            callback(None, error)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.cancel()
        if self.proxy is not None:
            for handler in self._handlers:
                self.proxy.disconnect(handler)
        self._handlers.clear()
        self._ready = self._signal = None


class ConfigurationWriter:
    """Serialize and coalesce edits; Start can wait for the latest saved snapshot."""
    def __init__(self, client):
        self.client = client
        self.inflight = False
        self.pending = None

    def submit(self, config, callback):
        callbacks = [] if self.pending is None else self.pending[1]
        callbacks.append(callback)
        self.pending = (dict(config), callbacks)
        self._flush()

    def _flush(self):
        if self.inflight or self.pending is None or self.client.closed:
            return
        config, callbacks = self.pending
        self.pending = None
        self.inflight = True
        def saved(success, error):
            self.inflight = False
            if not success and error is None:
                error = RuntimeError('The service could not save the settings')
            for callback in callbacks:
                if self.client.closed:
                    break
                callback(config, error)
            self._flush()
        self.client.call('SetConfig', saved, GLib.Variant('(s)', (json.dumps(config),)), timeout=60000)
