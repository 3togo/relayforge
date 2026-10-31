"""Managed editor feedback with fake transport and hidden data models only."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('managed_settings', ROOT / 'desktop/main.py')
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


class SettingsFeedbackTests(unittest.TestCase):
    def test_country_requirement_and_firewall_refusal_keep_administrator_policy_explicit(self):
        reason = 'Active firewalld is not supported; leave firewalld running'
        text = settings.capability_text({'native_policy': {'requested_country': 'US'},
                'startup_preflight': {'success': False, 'error': reason}})
        self.assertIn('Required radio country: US (already provisioned; no automatic changes)', text)
        self.assertIn(reason, text)
        self.assertNotIn('Required radio country:', settings.capability_text({'native_policy': {'requested_country': 'bad'}}))

    def test_security_description_names_transition_mode_and_ignores_unknown_modes(self):
        text = settings.capability_text({'native_security': {'mode': 'wpa2-wpa3-transition'}})
        self.assertIn('WPA2/WPA3 transition (WPA2 clients allowed)', text)
        self.assertNotIn('Security:', settings.capability_text({'native_security': {'mode': 'unknown'}}))
        self.assertNotIn('Security:', settings.capability_text({'native_security': 'malformed'}))

    def test_about_has_current_package_identity_without_service_access(self):
        for gnome in (False, True):
            with self.subTest(gnome=gnome), patch.object(settings.vendor, 'GNOME_DESKTOP', gnome), \
                    patch.object(settings, 'project_version', return_value='0.1.0~fixture1'), \
                    patch.object(settings.Gtk, 'AboutDialog') as gtk, \
                    patch.object(settings.vendor.Adw, 'AboutWindow') as adw, \
                    patch.object(settings.vendor, 'ServiceClient') as service:
                application = SimpleNamespace(props=SimpleNamespace(active_window=None))
                settings.HotspotAboutApp.do_activate(application)
                constructor = adw if gnome else gtk
                arguments = constructor.call_args.kwargs
                self.assertEqual(arguments['version'], '0.1.0~fixture1')
                self.assertEqual(arguments['application_name' if gnome else 'program_name'], 'RelayForge')
                self.assertEqual(arguments['website'], 'https://github.com/3togo/relayforge')
                constructor.return_value.present.assert_called_once()
                service.assert_not_called()

    def test_preflight_keeps_original_reason_and_gives_channel_guidance(self):
        reason = 'Concurrent sharing is not permitted on the current upstream channel'
        text = settings.capability_text({'ap_5ghz': False, 'current_sta_band': '5',
                'startup_preflight': {'success': False, 'error': reason}})
        self.assertIn(reason, text)
        self.assertIn('2.4 GHz', text)
        self.assertIn('5 GHz hotspot: No', text)
        self.assertNotIn('2.4 GHz', settings.capability_text({'startup_preflight': {'success': True}}))

    def test_application_uses_the_managed_window_constructor(self):
        self.assertIs(settings.vendor.HotspotSettingsWindow, settings.HotspotSettingsWindow)

    def window(self):
        window = SimpleNamespace(_load_generation=0, _closed=False, _loaded=False,
                _hotspot_busy=False, config_data={}, _set_form_sensitive=Mock(),
                _load_fallback_config=Mock(), _populate_fields=Mock(), _show_failure=Mock(),
                _clear_failure=Mock(), _refresh_status=Mock(), _fit_loaded_content=Mock(), _idle=Mock(),
                client=Mock(), entry_pass=Mock(), row_caps=Mock(), combo_wifi_iface=Mock(), combo_inet_iface=Mock())
        window._set_interface_choices = settings.HotspotSettingsWindow._set_interface_choices.__get__(window)
        for combo in (window.combo_wifi_iface, window.combo_inet_iface):
            combo.set_model.side_effect = lambda value, combo=combo: setattr(combo.get_model, 'return_value', value)
        return window

    def load(self, window, interfaces, unlock=True):
        window._unlock_requested = unlock
        with patch.object(settings.GLib, 'idle_add'):
            settings.HotspotSettingsWindow._load_data(window)
            window.client.call.call_args.args[1](interfaces, None)
            window.client.call.call_args.args[1]({'ap_5ghz': False}, None)
            if unlock:
                window.client.call.call_args.args[1]({'BACKEND': 'networkmanager'}, None)
            window._idle.assert_called_once_with(window._fit_loaded_content)

    def test_empty_inventory_has_no_invented_interfaces_and_explains_disabled_start(self):
        window = self.window()
        self.load(window, {'wifi_interfaces': [], 'all_interfaces': []})
        self.assertEqual(window.combo_wifi_iface.get_model().get_n_items(), 0)
        self.assertEqual(window.combo_inet_iface.get_model().get_n_items(), 0)
        self.assertFalse(window._loaded)
        window._show_failure.assert_called_once_with('No Wi-Fi adapter available',
                'Connect a Wi-Fi adapter, then choose Retry.', 'load')

    def test_observed_adapter_enables_the_form_after_protected_config_load(self):
        window = self.window()
        self.load(window, {'wifi_interfaces': ['demo0'], 'all_interfaces': ['demo0', 'eth0']})
        self.assertTrue(window._loaded)
        window._set_form_sensitive.assert_called_with(True)
        window._clear_failure.assert_called_with('load')

    def test_opening_settings_does_not_read_protected_configuration(self):
        window = self.window()
        self.load(window, {'wifi_interfaces':['demo0'], 'all_interfaces':['demo0']}, unlock=False)
        self.assertEqual([c.args[0] for c in window.client.call.call_args_list],
                         ['GetInterfaces', 'GetCapabilities'])
        self.assertFalse(window._loaded)
        window.entry_pass.set_text.assert_called_once_with('')
        self.assertIsInstance(window._show_failure.call_args.args[1], PermissionError)

    def test_only_explicit_unlock_permits_interactive_authorization(self):
        client = object.__new__(settings.SettingsServiceClient)
        client.closed = False; client.proxy = Mock(); client.generation = 1; client.cancel = Mock()
        client.call('GetConfig', Mock())
        self.assertEqual(client.proxy.call.call_args.args[2], settings.vendor.Gio.DBusCallFlags.NONE)
        client.call('GetConfig', Mock(), interactive=True)
        self.assertEqual(client.proxy.call.call_args.args[2],
                         settings.vendor.Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION)
        client.call('GetStatus', Mock())
        self.assertEqual(client.proxy.call.call_args.args[2], settings.vendor.Gio.DBusCallFlags.NONE)

    def test_stale_capability_reply_does_not_overwrite_new_configuration(self):
        window = self.window()
        settings.HotspotSettingsWindow._refresh_capabilities(window)
        callback = window.client.call.call_args.args[1]
        window.config_data = {'BACKEND': 'create_ap'}
        callback({'ap_5ghz': True}, None)
        window.row_caps.set_subtitle.assert_not_called()


if __name__ == '__main__':
    unittest.main()
