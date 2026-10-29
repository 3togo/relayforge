import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from gi.repository import GLib

spec = importlib.util.spec_from_file_location('adapter_limits', Path(__file__).resolve().parents[1]/'desktop/adapter_limits.py')
limits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limits)

class AdapterLimitTests(unittest.TestCase):
    def test_driver_ap_capability_decides_availability(self):
        for value in (0, 0x3f):
            self.assertTrue(limits.restricted(value))
        for value in (0x40, 0xff):
            self.assertFalse(limits.restricted(value))

    def test_multiple_adapters_out_of_order_failures_and_removal(self):
        class Bus:
            caps = {'wifi0': 0x40, 'wifi1': 0, 'wifi2': 0}
            failures = set()
            queued = []
            def call(self, name, path, interface, method, params, reply_type, flags, timeout, cancel, callback):
                if method == 'GetDeviceByIpIface':
                    key = params.unpack()[0]
                    result = GLib.Variant('(o)', ('/device/' + key,))
                else:
                    key = path.rsplit('/', 1)[1]
                    result = GLib.Variant('(v)', (GLib.Variant('u', self.caps[key]),))
                self.queued.append((callback, GLib.Error('Transient failure') if key in self.failures else result))
            def call_finish(self, result):
                if isinstance(result, GLib.Error):
                    raise result
                return result
            def drain(self):
                while self.queued:
                    callback, result = self.queued.pop()
                    callback(self, result)
        bus = Bus()
        changes = []
        observer = limits.AdapterLimit(lambda: changes.append(set(observer.blocked)))
        with patch.object(limits.Gio, 'bus_get', side_effect=lambda kind, cancel, cb: cb(None, None)), patch.object(limits.Gio, 'bus_get_finish', return_value=bus):
            observer.refresh(bus.caps)
            self.assertTrue(observer.pending)
            bus.drain()
            self.assertEqual(observer.blocked, {'wifi1', 'wifi2'})
            self.assertFalse(observer.pending)
            bus.failures = {'wifi1'}
            bus.caps['wifi0'] = 0
            bus.caps['wifi2'] = 0x40
            observer.refresh(bus.caps);bus.drain()
            self.assertEqual(observer.blocked, {'wifi0', 'wifi1'})
            bus.failures = set()
            bus.caps['wifi1'] = 0x40
            observer.refresh(['wifi1', 'wifi2']);bus.drain()
            self.assertEqual(observer.blocked, set())
            observer.refresh([])
            self.assertFalse(observer.pending)
            self.assertEqual(changes, [{'wifi1','wifi2'}, {'wifi0','wifi1'}, set()])
        observer.close()

    def test_reason_names_the_adapter_and_keeps_internet_source_available(self):
        text = limits.explanation('wifi2')
        self.assertIn('wifi2', text)
        self.assertIn('driver does not support', text)
        self.assertIn('Internet source', text)

if __name__ == '__main__':
    unittest.main()
