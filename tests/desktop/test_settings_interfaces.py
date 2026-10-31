"""Asynchronous settings behavior with fake transport; no host D-Bus or networking."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('hotspot_settings', Path(__file__).resolve().parents[2] / 'vendor/wifi-relay/settings/main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Window = module.HotspotSettingsWindow


class InterfaceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(
            _loading_fields=False, _hotspot_busy=False, _closed=False, _loaded=True,
            _save_timer=0, _pending_config=None, _status_revision=0, _closing_after_save=False,
            _status_timer=0, _retry_action=None, retry_button=Mock(), destroy=Mock(), _set_form_sensitive=Mock(),
            config_data={'NO_VIRT': '0', 'SHARE_METHOD': 'nat', 'WIFI_IFACE': 'wlo2', 'INTERNET_IFACE': 'wlo2'},
            row_status=Mock(), dbus_proxy=Mock(), combo_wifi_iface=Mock(), combo_inet_iface=Mock(), combo_band=Mock(),
            combo_backend=Mock(), switch_hotspot=Mock(), client=Mock(closed=False), add_toast=Mock(),
            _refresh_status=Mock(),
        )
        self.window.writer = module.ConfigurationWriter(self.window.client)
        for name in ('_collect_config', '_on_field_changed', '_flush_config', '_on_config_saved', '_on_hotspot_finished', '_close_resources', '_show_failure', '_clear_failure', '_retry_failed_action'):
            setattr(self.window, name, lambda *args, name=name: getattr(Window, name)(self.window, *args))
        self.window.combo_wifi_iface.get_selected_item.return_value.get_string.return_value = 'wlo2'
        self.window.combo_inet_iface.get_selected_item.return_value.get_string.return_value = 'wlo2'
        self.window.combo_band.get_selected.return_value = 0
        self.window.combo_backend.get_selected.return_value = 0
        for name, text in [('entry_ssid', 'Hotspot'), ('entry_pass', 'password123'),
                           ('entry_gateway', '192.168.12.1'), ('entry_channel', 'default')]:
            widget = Mock(); widget.get_text.return_value = text
            setattr(self.window, name, widget)
        for name in ('switch_hidden', 'switch_isolate', 'switch_80211n', 'switch_80211ac', 'switch_80211ax'):
            widget = Mock(); widget.get_active.return_value = False
            setattr(self.window, name, widget)
        patch.object(module.GLib, 'timeout_add', return_value=1).start()
        patch.object(module.GLib, 'source_remove').start()
        self.addCleanup(patch.stopall)

    def saved_config(self):
        method, _callback, parameters = self.window.client.call.call_args.args
        self.assertEqual(method, 'SetConfig')
        return json.loads(parameters.unpack()[0])

    def test_same_adapter_is_saved_for_upstream_and_hotspot(self):
        self.window._on_field_changed()
        self.window.client.call.assert_not_called()
        self.window._flush_config()
        saved = self.saved_config()
        self.assertEqual(saved['WIFI_IFACE'], 'wlo2')
        self.assertEqual(saved['INTERNET_IFACE'], 'wlo2')
        self.assertEqual(saved['NO_VIRT'], '0')
        self.assertEqual(saved['SHARE_METHOD'], 'nat')

    def test_empty_password_is_not_replaced_with_a_shared_default(self):
        self.window.entry_pass.get_text.return_value = ''
        self.assertEqual(self.window._collect_config()['PASSPHRASE'], '')

    def test_stop_preserves_edits_waiting_for_save(self):
        self.window._on_field_changed()
        pending = dict(self.window._pending_config)
        Window._on_switch_toggled(self.window, self.window.switch_hotspot, False)
        self.assertEqual(self.window._pending_config, pending)
        self.assertEqual(self.window._save_timer, 1)
        self.window._set_form_sensitive.assert_called_once_with(False)

    def test_loading_does_not_overwrite_configuration(self):
        self.window._loading_fields = True
        self.window._on_field_changed()
        self.assertIsNone(self.window._pending_config)
        self.window.client.call.assert_not_called()

    def test_networkmanager_backend_is_saved_from_selector(self):
        self.window.combo_backend.get_selected.return_value = 1
        self.window._on_field_changed(); self.window._flush_config()
        self.assertEqual(self.saved_config()['BACKEND'], 'networkmanager')

    def test_populating_fields_selects_saved_interfaces_without_saving(self):
        for row in (self.window.combo_wifi_iface, self.window.combo_inet_iface):
            row.get_model.return_value.get_n_items.return_value = 2
            row.get_model.return_value.get_string.side_effect = ['eth0', 'wlo2']
            row.set_selected.side_effect = lambda *_: self.window._on_field_changed()
        Window._populate_fields(self.window, self.window.config_data)
        self.window.combo_wifi_iface.set_selected.assert_called_once_with(1)
        self.window.combo_inet_iface.set_selected.assert_called_once_with(1)
        self.window.client.call.assert_not_called()
        self.assertFalse(self.window._loading_fields)

    def test_start_waits_for_visible_adapter_to_be_saved(self):
        self.window.config_data['WIFI_IFACE'] = 'wlan0'
        self.assertTrue(Window._on_switch_toggled(self.window, self.window.switch_hotspot, True))
        self.assertEqual(self.saved_config()['WIFI_IFACE'], 'wlo2')
        self.assertEqual(self.window.client.call.call_count, 1)
        self.window.client.call.call_args.args[1](True, None)
        self.assertEqual(self.window.client.call.call_args.args[0], 'Start')

    def test_start_is_blocked_until_protected_config_has_loaded(self):
        self.window._loaded = False
        Window._on_switch_toggled(self.window, self.window.switch_hotspot, True)
        self.window.client.call.assert_not_called()

    def test_failed_save_prevents_activation(self):
        Window._on_switch_toggled(self.window, self.window.switch_hotspot, True)
        self.window.client.call.call_args.args[1](None, RuntimeError('Save failed'))
        self.assertEqual(self.window.client.call.call_count, 1)
        self.assertFalse(self.window._hotspot_busy)
        self.window.add_toast.assert_called_once()

    def test_edits_are_coalesced_without_saving_partial_keystrokes(self):
        self.window.entry_ssid.get_text.return_value = 'H'
        self.window._on_field_changed()
        self.window.entry_ssid.get_text.return_value = 'Home relay'
        self.window._on_field_changed()
        self.window._flush_config()
        self.assertEqual(self.saved_config()['SSID'], 'Home relay')
        self.assertEqual(self.window.client.call.call_count, 1)

    def test_close_waits_for_latest_edits_to_be_saved(self):
        self.window.entry_ssid.get_text.return_value = 'Last edit'
        self.window._on_field_changed()
        self.assertTrue(Window._on_close(self.window))
        self.assertEqual(self.saved_config()['SSID'], 'Last edit')
        self.window.destroy.assert_not_called()
        self.window.client.call.call_args.args[1](True, None)
        self.window.destroy.assert_called_once()
        self.window.client.close.assert_called_once()

    def test_failed_save_on_close_keeps_window_and_edits(self):
        self.window._on_field_changed()
        Window._on_close(self.window)
        self.window.client.call.call_args.args[1](False, None)
        self.window.destroy.assert_not_called()
        self.assertFalse(self.window._closed)
        self.assertFalse(self.window._closing_after_save)

    def test_save_retry_uses_latest_fields_and_never_starts_hotspot(self):
        self.window._show_failure('Settings were not saved', RuntimeError('Disk full'), 'save')
        self.window._pending_config = {'SSID': 'Earlier edit'}
        self.window.entry_ssid.get_text.return_value = 'Latest edit'
        self.window._retry_failed_action()
        self.assertEqual(self.saved_config()['SSID'], 'Latest edit')
        self.assertIsNone(self.window._pending_config)
        self.window.client.call.call_args.args[1](True, None)
        self.assertIsNone(self.window._retry_action)
        self.window.retry_button.set_visible.assert_called_with(False)
        self.assertEqual(self.window.client.call.call_count, 1)

    def test_retry_is_blocked_while_saving_closing_or_operating(self):
        self.window._retry_action = 'save'
        for name in ['_closed', '_closing_after_save', '_hotspot_busy']:
            with self.subTest(name=name):
                setattr(self.window, name, True)
                self.window._retry_failed_action()
                self.window.client.call.assert_not_called()
                setattr(self.window, name, False)
        self.window.writer.inflight = True
        self.window._retry_failed_action()
        self.window.client.call.assert_not_called()

    def test_failed_stop_with_pending_edits_retries_stop_first(self):
        self.window._pending_config = {'SSID': 'Unsaved edit'}
        self.window._hotspot_busy = True
        self.window._on_hotspot_finished(False, None, False)
        self.assertEqual(self.window._retry_action, 'stop')
        self.assertFalse(self.window._hotspot_busy)
        self.assertEqual(self.window._pending_config, {'SSID': 'Unsaved edit'})
