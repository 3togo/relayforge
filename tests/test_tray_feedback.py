"""Exercise persistent failure feedback using real GTK menus without networking."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/wifi-relay/settings'))
spec = importlib.util.spec_from_file_location('relayforge_tray', ROOT / 'desktop/tray.py')
tray = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tray)


@unittest.skipUnless(Gtk.init_check()[0], 'GTK display required')
class TrayFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.app = tray.HotspotTray()
        self.app.indicator = Mock()
        self.app.proxy = Mock()
        self.addCleanup(self.dispose)

    def dispose(self):
        if hasattr(self.app, 'menu'):
            self.app.menu.destroy()

    def labels(self):
        return [item.get_child().get_text() for item in self.app.menu.get_children()
                if item.get_visible() and isinstance(item.get_child(), Gtk.Label)]

    def test_refresh_keeps_open_menu_and_action_widgets(self):
        self.app.status = {'active': True, 'ssid': 'Test', 'client_count': 0}
        self.app._render()
        self.app._render()
        self.app.indicator.set_icon_full.assert_called_once()
        menu = self.app.menu
        switch = menu.get_children()[1]
        settings = next(item for item in menu.get_children()
                        if item.get_label() == 'Hotspot Settings')
        self.app._request_change = Mock()
        for status in [dict(self.app.status), {'active': False},
                       {'active': True, 'ssid': 'Test', 'client_count': 1}]:
            self.app.status = status
            self.app._render()
            self.assertIs(self.app.menu, menu)
            self.assertIs(menu.get_children()[1], switch)
            self.assertIn(settings, menu.get_children())
        self.app.indicator.set_menu.assert_called_once_with(menu)
        self.app._request_change.assert_not_called()

    def test_client_updates_keep_actions_and_user_toggle_works(self):
        self.app.status = {'active': True, 'ssid': 'Test'}
        self.app.clients = [{'hostname': 'Phone', 'ip': '192.168.12.2'}]
        self.app._render()
        menu = self.app.menu
        switch = self.app.switch
        client_row = self.app.client_rows[0]
        self.app._request_change = Mock()
        self.app.clients = [{'hostname': 'Tablet', 'ip': '192.168.12.3'}]
        self.app._render()
        self.assertIs(self.app.client_rows[0], client_row)
        self.assertIn('Tablet (192.168.12.3)', self.labels())
        self.app.clients = []
        self.app._render()
        self.assertNotIn(client_row, menu.get_children())
        self.assertIs(self.app.menu, menu)
        self.app._request_change.assert_not_called()
        switch.set_active(False)
        self.app._request_change.assert_called_once_with('Stop')

    def test_failed_status_survives_refresh_and_explains_channel_remedy(self):
        data = {'active': False, 'state': 'failed', 'error':
                'Concurrent sharing is not permitted on the current upstream channel'}
        for _ in range(2):
            self.app._signal(None, None, 'StatusChanged', GLib.Variant('(s)', (json.dumps(data),)))
            self.assertIn('Wi-Fi Relay: Could not start', self.labels())
            self.assertIn(data['error'], self.labels())
            self.assertTrue(any('2.4 GHz' in text and 'enable the hotspot again' in text for text in self.labels()))
        self.assertIn(data['error'], self.app.indicator.set_icon_full.call_args.args[1])

    def test_arbitrary_backend_error_is_visible_without_channel_advice(self):
        self.app.status = {'active': False, 'error': 'Wi-Fi adapter removed'}
        self.app._render()
        self.assertIn('Wi-Fi adapter removed', self.labels())
        self.assertFalse(any('2.4 GHz' in text for text in self.labels()))

    def test_failed_request_remains_visible_until_success(self):
        self.app._error = Mock()
        self.app._operation_error('Authentication was denied')
        self.app.status = {'active': False}
        self.app._render()
        self.assertIn('Authentication was denied', self.labels())
        data = {'active': True, 'state': 'active'}
        self.app._signal(None, None, 'StatusChanged', GLib.Variant('(s)', (json.dumps(data),)))
        self.assertNotIn('Authentication was denied', self.labels())
        self.assertIn('Wi-Fi Relay: On', self.labels())

    def test_start_reply_failure_stays_visible_after_status_refresh(self):
        self.app._error = Mock()
        self.app._query = Mock()
        self.app.proxy.call_finish.return_value = GLib.Variant(
            '(s)', (json.dumps({'success': False, 'error': 'Access denied'}),))
        self.app._request_change('Start')
        callback = self.app.proxy.call.call_args.args[-1]
        callback(self.app.proxy, None)
        self.app._signal(None, None, 'StatusChanged',
                         GLib.Variant('(s)', (json.dumps({'active': False}),)))
        self.assertIn('Access denied', self.labels())
        self.app._error.assert_called_once_with('Access denied')

    def test_tray_start_stop_and_poll_never_allow_interactive_auth(self):
        from gi.repository import Gio
        self.app._render = Mock(); self.app._query = Mock()
        for method in ('Start', 'Stop'):
            self.app._request_change(method)
            self.assertEqual(self.app.proxy.call.call_args.args[2], Gio.DBusCallFlags.NONE)
        self.app.busy = False
        tray.HotspotTray._query(self.app)
        self.assertEqual(self.app.proxy.call.call_args.args[0], 'GetStatus')
        self.assertEqual(self.app.proxy.call.call_args.args[2], Gio.DBusCallFlags.NONE)

    def test_unavailable_service_explains_disabled_switch(self):
        self.app.status = {'active': False, 'unavailable': True}
        self.app._render()
        switch = self.app.menu.get_children()[1]
        self.assertFalse(switch.get_sensitive())
        self.assertIn('unavailable', switch.get_tooltip_text())
        self.assertTrue(any('Open Hotspot Settings' in text for text in self.labels()))

    def test_transition_explains_disabled_switch(self):
        self.app.busy = True
        self.app.status = {'active': False, 'state': 'connecting'}
        self.app._render()
        switch = self.app.menu.get_children()[1]
        self.assertFalse(switch.get_sensitive())
        self.assertIn('Wait', switch.get_tooltip_text())
        self.assertIn('Starting hotspot; waiting for Wi-Fi…', self.labels())

    def test_stale_failure_does_not_override_transition_headline(self):
        self.app.busy = True
        self.app.status = {'active': False, 'state': 'connecting', 'error': 'Prior error'}
        self.app._render()
        self.assertIn('Wi-Fi Relay: Connecting…', self.labels())


if __name__ == '__main__':
    unittest.main()
