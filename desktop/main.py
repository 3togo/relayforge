#!/usr/bin/python3
"""RelayForge adaptations of the retained MIT-licensed Wi-Fi Relay editor.

Keep the original editor as vendor_main.py in installed packages. The local authorization and capability adaptations extend the incoming editor
in upstream_main.py, retaining its desktop lifecycle and adapter-limit fixes.
"""
import importlib.util
import json
from pathlib import Path
import sys

source = Path(__file__).with_name('upstream_main.py')
if not source.exists():
    raise FileNotFoundError('RelayForge settings base upstream_main.py is missing')
spec = importlib.util.spec_from_file_location('_relayforge_vendor_settings', source)
vendor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vendor)
Gtk, GLib = vendor.Gtk, vendor.GLib


def project_version():
    metadata = Path(__file__).with_name('project.json')
    if metadata.exists():
        return json.loads(metadata.read_text())['version']
    # Development source runs use the workspace version; installed packages
    # carry their exact package version and need no TOML runtime dependency.
    import tomllib
    return tomllib.loads((Path(__file__).resolve().parents[1] / 'Cargo.toml').read_text())['workspace']['package']['version']


def explain_failure(error):
    message = str(error)
    if 'Concurrent sharing is not permitted on the current upstream channel' in message:
        return message + '\nTry a 2.4 GHz Wi-Fi network, then enable the hotspot again.'
    if 'configured overrides' in message:
        return message + '\nReview backend options; NetworkManager requires its supported defaults.'
    if 'declines bridge mode' in message:
        return message + '\nSelect NAT sharing; native bridge support is not available yet.'
    if 'preserves upstream and regulatory settings' in message:
        return message + '\nReconnect to a permitted Wi-Fi channel explicitly and remove country overrides.'
    if 'preserves the upstream' in message:
        return message + '\nReconnect to a permitted Wi-Fi channel explicitly before starting the hotspot.'
    return message


def capability_text(data):
    text = f"5 GHz hotspot: {'Yes' if data.get('ap_5ghz') else 'No'} | Current Wi-Fi band: {data.get('current_sta_band') or 'Not connected'}"
    security = data.get('native_security')
    if isinstance(security, dict):
        modes = {'wpa1-personal': 'WPA1-Personal (legacy)',
                 'wpa2-personal': 'WPA2-Personal',
                 'wpa2-wpa3-transition': 'WPA2/WPA3 transition (WPA2 clients allowed)'}
        label = modes.get(security.get('mode'))
        if label:
            text += '\nSecurity: ' + label
    preflight = data.get('startup_preflight')
    policy = data.get('native_policy')
    if isinstance(policy, dict):
        country = policy.get('requested_country')
        if isinstance(country, str) and len(country) == 2 and country.isascii() and country.isalpha() and country.isupper():
            text += '\nRequired radio country: ' + country + ' (already provisioned; no automatic changes)'
    if isinstance(preflight, dict) and preflight.get('success') is False:
        text += '\n' + explain_failure(preflight.get('error') or 'Current radio/configuration cannot start sharing.')
    return text


class SettingsServiceClient(vendor.ServiceClient):
    """Only an explicit Unlock/Authorize button may permit an agent prompt."""
    def call(self, method, callback, parameters=None, timeout=10000, *, interactive=False):
        interactive = interactive or (method == 'SetConfig' and getattr(self, 'manual_authorization', False))
        if not interactive:
            return super().call(method, callback, parameters, timeout)
        if self.closed:
            return
        if self.proxy is None:
            callback(None, RuntimeError('Hotspot service is unavailable'))
            return
        try:
            from service_client import decode_reply
        except ModuleNotFoundError:
            from settings.service_client import decode_reply
        generation = self.generation

        def finished(proxy, result):
            data, error = None, None
            try:
                data = decode_reply(method, proxy.call_finish(result))
            except (GLib.Error, ValueError, TypeError, UnicodeError) as failure:
                error = failure
            if self.closed:
                return
            if generation != self.generation:
                data, error = None, RuntimeError('Hotspot service restarted; retry the request')
            callback(data, error)
        try:
            self.proxy.call(method, parameters, vendor.Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
                            timeout, self.cancel, finished)
        except GLib.Error as error:
            callback(None, error)


vendor.ServiceClient = SettingsServiceClient


class HotspotSettingsWindow(vendor.HotspotSettingsWindow):
    def _show_failure(self, title, error, action):
        locked = isinstance(error, PermissionError) or 'AccessDenied' in str(error)
        self._configuration_locked = locked and action == 'load'
        if self._configuration_locked:
            title = 'Settings locked'
            error = 'Choose Unlock settings to view or change saved hotspot credentials. Opening this window does not request a password.'
        super()._show_failure(title, explain_failure(error), action)
        self.retry_button.set_label('Unlock settings' if self._configuration_locked else
                                    'Authorize and retry save' if locked and action == 'save' else 'Retry')

    def _retry_failed_action(self, *args):
        if self._closed or self._hotspot_busy or self.writer.inflight or self._closing_after_save:
            return
        if getattr(self, '_configuration_locked', False) and self._retry_action == 'load':
            self._unlock_requested = True
            self.retry_button.set_sensitive(False)
            self._load_data()
            return
        # A failed automatic save stays noninteractive. Only its explicit retry
        # button can authorize that save, and the permission is not sticky.
        self.client.manual_authorization = True
        try:
            super()._retry_failed_action(*args)
        finally:
            self.client.manual_authorization = False

    def _refresh_capabilities(self):
        if self._closed or self._hotspot_busy:
            return
        revision = getattr(self, '_capability_revision', 0) + 1
        self._capability_revision = revision
        config = dict(self.config_data)

        def loaded(data, error):
            if self._closed or revision != self._capability_revision or config != self.config_data:
                return
            if error:
                self.row_caps.set_subtitle('Capabilities unavailable: ' + str(error))
            else:
                self.capabilities = data
                self.row_caps.set_subtitle(capability_text(data))
        self.client.call('GetCapabilities', loaded)

    def _on_config_saved(self, config, error):
        super()._on_config_saved(config, error)
        if not error:
            self._refresh_capabilities()

    def _on_hotspot_finished(self, data, error, starting):
        super()._on_hotspot_finished(data, error, starting)
        self._refresh_capabilities()

    def _load_data(self):
        unlock = getattr(self, '_unlock_requested', False)
        self._unlock_requested = False
        self._load_generation += 1
        generation = self._load_generation
        self._loading_fields = True
        self._set_form_sensitive(False)

        def current():
            return not self._closed and generation == self._load_generation

        def interfaces_loaded(data, error):
            if not current():
                return
            if error:
                self._load_fallback_config()
            else:
                self._set_interface_choices(data)
            self.client.call('GetCapabilities', capabilities_loaded)

        def capabilities_loaded(data, error):
            if not current():
                return
            if not error:
                self.capabilities = data
                self.row_caps.set_subtitle(capability_text(data))
            if unlock:
                self.client.call('GetConfig', config_loaded, timeout=60000, interactive=True)
            else:
                config_loaded(None, PermissionError('Settings are locked'))

        def config_loaded(data, error):
            if not current():
                return
            if error:
                self._load_fallback_config()
                if isinstance(error, PermissionError) or 'AccessDenied' in str(error):
                    self.entry_pass.set_text('')
            else:
                self._populate_fields(data)
            self._loading_fields = False
            has_wifi = self.combo_wifi_iface.get_model().get_n_items() > 0
            self._loaded = error is None and has_wifi
            self._set_form_sensitive(self._loaded)
            if error:
                self._show_failure('Settings could not be loaded', error, 'load')
            elif not has_wifi:
                self._show_failure('No Wi-Fi adapter available', 'Connect a Wi-Fi adapter, then choose Retry.', 'load')
            else:
                self._clear_failure('load')
            self._refresh_status()
            self._idle(self._fit_loaded_content)

        self.client.call('GetInterfaces', interfaces_loaded)

    def _load_fallback_config(self):
        # A locked window may inspect devices, never saved credential files.
        devices = Path('/sys/class/net')
        all_interfaces = sorted(p.name for p in devices.iterdir() if p.name != 'lo')
        wifi = [name for name in all_interfaces if
                (devices/name/'wireless').exists() or (devices/name/'phy80211').exists()]
        self._set_interface_choices({'wifi_interfaces': wifi, 'all_interfaces': all_interfaces})
        self._populate_fields({})
        self.entry_pass.set_text('')


# The original application's activation method resolves its window class from
# its own module. Explicitly bind that constructor to this small adaptation.
vendor.HotspotSettingsWindow = HotspotSettingsWindow
HotspotSettingsApp = vendor.HotspotSettingsApp


class HotspotAboutApp(vendor.HotspotAboutApp):
    """About remains independent of configuration, authorization and service I/O."""
    def do_activate(self):
        win = self.props.active_window
        if not win:
            common = dict(application=self, version=project_version(),
                          copyright='© 2026 RelayForge and Wi-Fi Relay contributors',
                          license_type=Gtk.License.MIT_X11,
                          website='https://github.com/3togo/relayforge',
                          comments='Linux Wi-Fi sharing with a native service and optional desktop controls.')
            credits = ['Erhan Zeyrek', 'Wi-Fi Relay contributors', 'RelayForge contributors']
            third_party = ['create_ap — oblique and lakinduaksh (BSD-2-Clause)',
                           'QR Code generator — Project Nayuki (MIT)']
            if vendor.GNOME_DESKTOP:
                win = vendor.Adw.AboutWindow(application_name='RelayForge',
                    application_icon=vendor.BUS_NAME, developer_name='RelayForge contributors',
                    developers=credits, issue_url='https://github.com/3togo/relayforge/issues', **common)
                win.add_acknowledgement_section('Third-party components', third_party)
            else:
                win = Gtk.AboutDialog(program_name='RelayForge', logo_icon_name=vendor.BUS_NAME,
                                      authors=credits, **common)
                win.add_credit_section('Third-party components', third_party)
        win.present()

if __name__ == '__main__':
    sys.exit(HotspotSettingsApp().run(sys.argv))
