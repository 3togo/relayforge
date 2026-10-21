#!/usr/bin/env python3
"""
Wi-Fi Relay Settings App (desktop GTK / GNOME Libadwaita)
Desktop settings for configuring Wi-Fi Hotspot, managing AP settings,
viewing connected devices and generating QR connection codes.
"""

import sys
import os
import json
import re
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk

try:
    from startup import get_auto_start, set_auto_start, is_gnome, launch_tray, desktop_integration_available
except ModuleNotFoundError:
    from settings.startup import get_auto_start, set_auto_start, is_gnome, launch_tray, desktop_integration_available

try:
    from service_client import ServiceClient, ConfigurationWriter
except ModuleNotFoundError:
    from settings.service_client import ServiceClient, ConfigurationWriter

try:
    from wifi_qr import wifi_payload, qr_rgb
except ModuleNotFoundError:
    from settings.wifi_qr import wifi_payload, qr_rgb

BUS_NAME = "io.github.erhanzeyrek.WifiHotspot"
OBJECT_PATH = "/io/github/erhanzeyrek/WifiHotspot"
INTERFACE_NAME = "io.github.erhanzeyrek.WifiHotspot"


def get_default_ssid():
    """Generate default SSID from machine hostname."""
    try:
        import socket
        name = socket.gethostname().strip()
        if name and name.lower() not in ("localhost", "localhost.localdomain", "(none)"):
            return f"{name}-Hotspot"
    except Exception:
        pass
    return "Hotspot"


GNOME_DESKTOP = is_gnome()
if not GNOME_DESKTOP:
    os.environ.setdefault('GTK_CSD', '0')
ApplicationBase = Adw.Application if GNOME_DESKTOP else Gtk.Application
WindowBase = Adw.ApplicationWindow if GNOME_DESKTOP else Gtk.ApplicationWindow


class NativeAppearance:
    """Bridge KDE's portal preference while retaining the native GTK theme/fonts."""
    def __init__(self):
        self.cancellable = Gio.Cancellable()
        self.proxy = None
        self.handlers = []
        self.settings_handlers = []
        self.preference = 0
        if not GNOME_DESKTOP and hasattr(Gtk, 'InterfaceColorScheme'):
            self.settings = Gtk.Settings.get_default()
            self.settings_handlers = [self.settings.connect('notify::gtk-theme-name', self._theme_changed),
                self.settings.connect('notify::gtk-application-prefer-dark-theme', self._theme_changed)]
            self._theme_changed()
        if not GNOME_DESKTOP and 'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper().split(':'):
            Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None, 'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
                'org.freedesktop.portal.Settings', self.cancellable, self._connected)

    def _connected(self, _source, result):
        try:
            proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error:
            return
        if self.cancellable.is_cancelled():
            return
        self.proxy = proxy
        self.revision = 0
        self.handlers = [proxy.connect('g-signal', self._changed),
                         proxy.connect('notify::g-name-owner', self._owner_changed)]
        self._owner_changed()

    def _owner_changed(self, *_args):
        self.revision += 1
        if self.proxy.get_name_owner():
            self._read('ReadOne')
        else:
            self._apply(0)

    def _read(self, method):
        revision = self.revision
        def loaded(proxy, result):
            try:
                value = proxy.call_finish(result).unpack()[0]
                while isinstance(value, GLib.Variant):
                    value = value.unpack()
            except GLib.Error:
                if method == 'ReadOne' and not self.cancellable.is_cancelled():
                    self._read('Read')
                return
            if not self.cancellable.is_cancelled() and self.revision == revision:
                self._apply(value)
        self.proxy.call(method, GLib.Variant('(ss)', ('org.freedesktop.appearance', 'color-scheme')),
                        Gio.DBusCallFlags.NO_AUTO_START, 1000, self.cancellable, loaded)

    def _changed(self, _proxy, _sender, signal, parameters):
        values = parameters.unpack()
        if (signal == 'SettingChanged' and len(values) == 3
                and values[:2] == ('org.freedesktop.appearance', 'color-scheme')):
            self.revision += 1
            self._apply(values[2])

    def _theme_changed(self, *_args):
        settings = Gtk.Settings.get_default()
        theme_dark = bool(re.search(r'(^|[-_\s])dark($|[-_\s])', settings.props.gtk_theme_name, re.I))
        dark = self.preference == 1 or (self.preference != 2 and
               (theme_dark or settings.props.gtk_application_prefer_dark_theme))
        scheme = Gtk.InterfaceColorScheme.DARK if dark else Gtk.InterfaceColorScheme.LIGHT
        if settings.props.gtk_interface_color_scheme != scheme:
            settings.props.gtk_interface_color_scheme = scheme

    def _apply(self, preference):
        self.preference = preference
        settings = Gtk.Settings.get_default()
        if hasattr(Gtk, 'InterfaceColorScheme'):
            self._theme_changed()
        elif preference in (1, 2):
            settings.set_property('gtk-application-prefer-dark-theme', preference == 1)
        else:
            settings.reset_property('gtk-application-prefer-dark-theme')

    def close(self):
        self.cancellable.cancel()
        if self.proxy:
            for handler in self.handlers:
                self.proxy.disconnect(handler)
        self.handlers = []
        for handler in self.settings_handlers:
            self.settings.disconnect(handler)
        self.settings_handlers = []


class HotspotApplication(ApplicationBase):
    def do_startup(self):
        ApplicationBase.do_startup(self)
        self.appearance = NativeAppearance()

    def do_shutdown(self):
        self.appearance.close()
        ApplicationBase.do_shutdown(self)


class NativeMessageDialog(Gtk.Window):
    def __init__(self, transient_for, heading, body):
        super().__init__(transient_for=transient_for, modal=True, title=heading)
        self.set_default_size(380, -1)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(content)
        content.append(Gtk.Label(label=body, wrap=True, max_width_chars=55, selectable=True))
        self.extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.extra)
        self.buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        content.append(self.buttons)
        self.extra_child = None

    def set_extra_child(self, child):
        if self.extra_child:
            self.extra.remove(self.extra_child)
        self.extra_child = child
        if child:
            self.extra.append(child)

    def get_extra_child(self):
        return self.extra_child

    def add_response(self, _response, label):
        button = Gtk.Button(label=label)
        button.connect('clicked', lambda *_args: self.close())
        self.buttons.append(button)


MessageDialog = Adw.MessageDialog if GNOME_DESKTOP else NativeMessageDialog


class SettingsDropDown(Gtk.DropDown):
    def set_subtitle(self, text):
        self.set_tooltip_text(text)


class SettingsDetail(Gtk.Box):
    """Compact status text with optional controls, without a preferences card."""
    def __init__(self, title, subtitle):
        super().__init__(spacing=12)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True,
                         valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label=title, xalign=0, wrap=True)
        self.subtitle = Gtk.Label(label=subtitle, xalign=0, wrap=True, max_width_chars=60)
        self.subtitle.add_css_class("dim-label")
        labels.append(self.title)
        labels.append(self.subtitle)
        self.append(labels)

    def set_title(self, title):
        self.title.set_text(title)

    def get_title(self):
        return self.title.get_text()

    def set_subtitle(self, subtitle):
        self.subtitle.set_text(subtitle)

    def get_subtitle(self):
        return self.subtitle.get_text()

    def add_suffix(self, widget):
        self.append(widget)


class HotspotSettingsWindow(WindowBase):
    def __init__(self, app):
        super().__init__(application=app, title="Wi-Fi Relay Settings")
        self.set_default_size(720, -1)
        self.app = app
        self.dbus_proxy = None
        self.config_data = {}
        self.interfaces = {"wifi_interfaces": ["wlan0"], "all_interfaces": ["wlan0", "eth0"]}
        self.capabilities = {}
        self._hotspot_busy = False
        self._loading_fields = True
        self._initial_size_fitted = False

        self._closed = False
        self._loaded = False
        self._load_generation = 0
        self._status_pending = False
        self._clients_pending = False
        self._status_revision = 0
        self._save_timer = 0
        self._pending_config = None
        self._closing_after_save = False
        self._retry_action = None
        self._build_ui()
        self._fit_default_size()
        self._set_form_sensitive(False)
        self.switch_hotspot.set_sensitive(False)
        self.client = ServiceClient(self._on_client_ready)
        self.writer = ConfigurationWriter(self.client)
        self._status_timer = GLib.timeout_add_seconds(3, self._refresh_status)
        self.connect("close-request", self._on_close)

    def _on_client_ready(self, error):
        if self._closed:
            return
        self.dbus_proxy = self.client.proxy
        if error:
            self._load_fallback_config()
            self._loading_fields = False
            self._show_failure("Hotspot service unavailable", error, "load")
            GLib.idle_add(self._fit_loaded_content)
            return
        if not self._loaded:
            self._load_data()
        else:
            self._refresh_status()

    def _on_close(self, *_args):
        if self._closed:
            return False
        if self._closing_after_save:
            return True
        if self._pending_config is not None or self.writer.inflight:
            config = self._collect_config()
            if config is not None:
                self._closing_after_save = True
                self._set_form_sensitive(False)
                if self._save_timer:
                    GLib.source_remove(self._save_timer)
                    self._save_timer = 0
                self._pending_config = None
                def saved(config, error):
                    self._closing_after_save = False
                    if error:
                        self._pending_config = dict(config)
                        self._set_form_sensitive(True)
                        self._show_failure("Settings were not saved", error, "save")
                        self.add_toast(str(error))
                        return
                    self._close_resources()
                    self.destroy()
                self.writer.submit(config, saved)
                return True
        self._close_resources()
        return False

    def _close_resources(self):
        self._closed = True
        for source in (self._status_timer, self._save_timer):
            if source:
                GLib.source_remove(source)
        self._status_timer = self._save_timer = 0
        self.client.close()

    def _show_failure(self, title, error, action):
        self._retry_action = action
        self.row_status.set_title(title)
        self.row_status.set_subtitle(str(error))
        self.retry_button.set_visible(True)
        self.retry_button.set_sensitive(True)

    def _clear_failure(self, *actions):
        if self._retry_action in actions:
            self._retry_action = None
            self.retry_button.set_visible(False)
            self.row_status.set_title("Hotspot")

    def _retry_failed_action(self, *_args):
        if self._closed or self._hotspot_busy or self.writer.inflight or self._closing_after_save:
            return
        action = self._retry_action
        self.retry_button.set_sensitive(False)
        if action == "save":
            config = self._collect_config()
            if config is None:
                self.retry_button.set_sensitive(True)
                return
            if self._save_timer:
                GLib.source_remove(self._save_timer)
                self._save_timer = 0
            self._pending_config = None
            self.writer.submit(config, self._on_config_saved)
        elif action == "load":
            self._load_data()
        elif action in {"start", "stop"}:
            self._on_switch_toggled(self.switch_hotspot, action == "start")
        elif action == "status":
            self._refresh_status()

    def _set_form_sensitive(self, enabled):
        for name in ('combo_backend', 'combo_wifi_iface', 'combo_inet_iface', 'combo_band',
                     'entry_ssid', 'entry_pass', 'entry_gateway', 'entry_channel',
                     'switch_hidden', 'switch_isolate', 'switch_80211n', 'switch_80211ac', 'switch_80211ax'):
            getattr(self, name).set_sensitive(enabled)
        if enabled and self.config_data.get('BACKEND') == 'networkmanager':
            self.combo_inet_iface.set_sensitive(False)

    def _fit_loaded_content(self):
        if not self._closed and not self._initial_size_fitted:
            self._fit_default_size()
            self._initial_size_fitted = True
        return GLib.SOURCE_REMOVE

    def _fit_default_size(self):
        monitors = self.get_display().get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return
        geometry = monitor.get_geometry()
        width = min(720, max(1, geometry.width - 32))
        _minimum, natural, _minimum_baseline, _natural_baseline = self.measure(Gtk.Orientation.VERTICAL, width)
        # Leave room for desktop panels and window-manager decorations.
        self.set_default_size(width, min(natural, max(1, geometry.height - 96)))

    def add_toast(self, message):
        if self.gnome_layout:
            self.toast_overlay.add_toast(Adw.Toast.new(message))
        else:
            self.notice_label.set_text(message)
            self.notice_bar.set_revealed(True)

    def _build_ui(self):
        # Match the connection editor's notebook, 12px page margins and 6px rows.
        self.gnome_layout = is_gnome()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        if self.gnome_layout:
            self.toast_overlay = Adw.ToastOverlay()
            self.set_content(self.toast_overlay)
            toolbar = Adw.ToolbarView()
            self.toast_overlay.set_child(toolbar)
            header = Adw.HeaderBar()
            toolbar.add_top_bar(header)
            toolbar.set_content(content)
        else:
            # No Adwaita widgets or client-side header bar: GTK and the window
            # manager retain the desktop theme, fonts, accent and decorations.
            self.set_child(content)
            self.set_icon_name(BUS_NAME)
            self.notice_bar = Gtk.InfoBar(show_close_button=True, revealed=False,
                                           message_type=Gtk.MessageType.WARNING)
            self.notice_label = Gtk.Label(wrap=True, xalign=0)
            self.notice_bar.add_child(self.notice_label)
            self.notice_bar.connect('response', lambda *_args: self.notice_bar.set_revealed(False))
            content.append(self.notice_bar)

        self.row_status = SettingsDetail(title="Hotspot", subtitle="Checking connection…")
        self.switch_hotspot = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch_hotspot.update_property([Gtk.AccessibleProperty.LABEL], ["Hotspot"])
        self.switch_hotspot.connect("state-set", self._on_switch_toggled)
        self.row_status.add_suffix(self.switch_hotspot)
        self.retry_button = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER, visible=False)
        self.retry_button.connect("clicked", self._retry_failed_action)
        self.row_status.add_suffix(self.retry_button)
        content.append(self.row_status)

        self.gnome_lists = {}
        if self.gnome_layout:
            self.view_stack = Adw.ViewStack(vexpand=True, vhomogeneous=True)
            header.set_title_widget(Adw.ViewSwitcher(stack=self.view_stack))
            content.append(self.view_stack)
        else:
            self.notebook = Gtk.Notebook(scrollable=True, vexpand=True)
            content.append(self.notebook)
        self.page_scrolls = []
        self.field_labels = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        def page(title):
            grid = Gtk.Grid(row_spacing=6, column_spacing=12,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                       vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                       propagate_natural_height=True)
            scroll.set_child(grid)
            if self.gnome_layout:
                rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
                rows.add_css_class("boxed-list")
                grid.attach(rows, 0, 0, 2, 1)
                self.gnome_lists[grid] = rows
                icons = {"Wi-Fi": "network-wireless-symbolic", "Sharing": "network-transmit-receive-symbolic",
                         "Advanced": "emblem-system-symbolic", "Devices": "network-workgroup-symbolic"}
                self.view_stack.add_titled_with_icon(scroll, title, title, icons[title])
            else:
                self.notebook.append_page(scroll, Gtk.Label(label=title))
            self.page_scrolls.append(scroll)
            return grid

        def field(grid, row, title, widget, hint=None):
            widget.update_property([Gtk.AccessibleProperty.LABEL], [title.replace("_", "")])
            widget.set_hexpand(True)
            if hint:
                widget.set_tooltip_text(hint)
            if self.gnome_layout:
                widget.set_hexpand(False)
                widget.set_valign(Gtk.Align.CENTER)
                if isinstance(widget, (Gtk.Entry, Gtk.PasswordEntry)):
                    widget.set_size_request(220, -1)
                settings_row = Adw.ActionRow(title=title, use_underline=True)
                settings_row.add_suffix(widget)
                settings_row.set_activatable_widget(widget)
                self.gnome_lists[grid].append(settings_row)
            else:
                label = Gtk.Label(label=title, use_underline=True, xalign=1, valign=Gtk.Align.CENTER)
                label.set_mnemonic_widget(widget)
                self.field_labels.add_widget(label)
                grid.attach(label, 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)
            return widget

        def option(grid, row, title, hint=None):
            if self.gnome_layout:
                check = Adw.SwitchRow(title=title, use_underline=True)
                self.gnome_lists[grid].append(check)
            else:
                check = Gtk.CheckButton(label=title, use_underline=True)
                grid.attach(check, 1, row, 1, 1)
            if hint:
                check.set_tooltip_text(hint)
            check.connect("notify::active", self._on_field_changed)
            return check

        wifi = page("Wi-Fi")
        self.entry_ssid = field(wifi, 0, "_SSID", Gtk.Entry())
        self.entry_pass = field(wifi, 1, "_Password", Gtk.PasswordEntry(show_peek_icon=True))
        self.combo_wifi_iface = field(wifi, 2, "_Device", SettingsDropDown(),
                                     "Wi-Fi adapter used to broadcast the hotspot")
        self.combo_band = field(wifi, 3, "_Band", SettingsDropDown())
        self.combo_band.set_model(Gtk.StringList.new(["Automatic", "2.4 GHz", "5 GHz"]))
        self.entry_channel = field(wifi, 4, "_Channel", Gtk.Entry(), "Use default to choose a channel automatically")
        self.switch_hidden = option(wifi, 5, "_Hidden network", "Do not broadcast the network name")

        sharing = page("Sharing")
        self.combo_inet_iface = field(sharing, 0, "Share _from", SettingsDropDown(),
                                      "Choose the same Wi-Fi adapter for simultaneous Wi-Fi + hotspot")
        self.entry_gateway = field(sharing, 1, "_Gateway", Gtk.Entry())
        self.switch_isolate = option(sharing, 2, "_Isolate connected devices",
                                     "Prevent hotspot clients from communicating with each other")
        hint = Gtk.Label(label="Connected devices share the selected internet connection.",
                         xalign=0, wrap=True, margin_top=6)
        hint.add_css_class("dim-label")
        sharing.attach(hint, 0, 3, 2, 1)

        advanced = page("Advanced")
        self.combo_backend = field(advanced, 0, "Hotspot _backend", SettingsDropDown())
        self.combo_backend.set_model(Gtk.StringList.new(["create_ap", "NetworkManager (Experimental)"]))
        backend_hint = Gtk.Label(label="The experimental NetworkManager backend follows the host’s default route.",
                                 xalign=0, wrap=True, margin_bottom=6, max_width_chars=55)
        backend_hint.add_css_class("dim-label")
        advanced.attach(backend_hint, 0, 1, 2, 1)
        self.switch_80211n = option(advanced, 2, "Wi-Fi _4 (802.11n)")
        self.switch_80211ac = option(advanced, 3, "Wi-Fi _5 (802.11ac)", "Requires a 5 GHz channel")
        self.switch_80211ax = option(advanced, 4, "Wi-Fi _6 (802.11ax)")
        self.row_caps = SettingsDetail(title="Hardware capabilities", subtitle="Detecting…")
        self.row_caps.set_margin_top(6)
        advanced.attach(self.row_caps, 0, 5, 2, 1)

        devices = page("Devices")
        if self.gnome_layout:
            devices.remove(self.gnome_lists[devices])
        share = SettingsDetail(title="Connect a device", subtitle="Scan a QR code to join this hotspot")
        qr = Gtk.Button(label="Show _QR Code", use_underline=True, valign=Gtk.Align.CENTER)
        qr.connect("clicked", self._on_show_qr)
        share.add_suffix(qr)
        devices.attach(share, 0, 0, 2, 1)
        self.row_no_clients = SettingsDetail(title="No devices connected",
                                            subtitle="Connected devices will be listed here")
        self.grp_clients_list = Gtk.Frame(label="Connected devices", child=self.row_no_clients,
                                          margin_top=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.row_no_clients, "set_margin_" + side)(12)
        devices.attach(self.grp_clients_list, 0, 1, 2, 1)

        for entry in (self.entry_ssid, self.entry_pass, self.entry_gateway, self.entry_channel):
            entry.connect("changed", self._on_field_changed)
        for combo in (self.combo_backend, self.combo_wifi_iface, self.combo_inet_iface, self.combo_band):
            combo.connect("notify::selected", self._on_field_changed)

        controls_available = desktop_integration_available()
        self.switch_startup = Gtk.CheckButton(label="Start desktop controls at _login", use_underline=True,
            active=get_auto_start() if controls_available else False, sensitive=controls_available)
        self.switch_startup.set_tooltip_text("Show desktop controls when you log in" if controls_available
                                             else "Install the optional GNOME or tray integration for your desktop")
        self.switch_startup.connect("notify::active", self._on_startup_changed)
        content.append(self.switch_startup)
        footer = Gtk.Box(spacing=12)
        saved = Gtk.Label(label="Changes are saved automatically", xalign=0, hexpand=True)
        saved.add_css_class("dim-label")
        footer.append(saved)
        close = Gtk.Button(label="_Close", use_underline=True)
        close.connect("clicked", lambda *_args: self.close())
        footer.append(close)
        content.append(footer)

    # ---------------- D-Bus & Data Operations ---------------- #

    def _on_startup_changed(self, row, _param):
        enabled = row.get_active()
        try:
            if not desktop_integration_available():
                raise RuntimeError("Desktop controls are not installed for this session")
            set_auto_start(enabled, Gio.Settings.new("org.gnome.shell") if is_gnome() else None, Gio.Settings.sync)
            if enabled and not is_gnome():
                launch_tray()
        except Exception as error:
            row.handler_block_by_func(self._on_startup_changed)
            row.set_active(get_auto_start())
            row.handler_unblock_by_func(self._on_startup_changed)
            dialog = MessageDialog(transient_for=self, heading="Could not save startup setting", body=str(error))
            dialog.add_response("ok", "Close")
            dialog.present()

    def _load_data(self):
        self._load_generation += 1
        generation = self._load_generation
        self._loading_fields = True
        self._set_form_sensitive(False)
        def current():
            return not self._closed and generation == self._load_generation
        def interfaces_loaded(data, error):
            if not current():
                return
            if not error:
                self.interfaces = data
                self.combo_wifi_iface.set_model(Gtk.StringList.new(data["wifi_interfaces"] or ["wlan0"]))
                self.combo_inet_iface.set_model(Gtk.StringList.new(data["all_interfaces"] or ["wlan0"]))
            else:
                self._load_fallback_config()
            self.client.call("GetCapabilities", capabilities_loaded)

        def capabilities_loaded(data, error):
            if not current():
                return
            if not error:
                self.capabilities = data
                ap_5g = "Yes" if data.get("ap_5ghz") else "No"
                band = data.get("current_sta_band", "Unknown")
                self.row_caps.set_subtitle(f"5GHz AP Support: {ap_5g} | Current Wi-Fi Band: {band} GHz")
            self.client.call("GetConfig", config_loaded, timeout=60000)

        def config_loaded(data, error):
            if not current():
                return
            if error:
                self._load_fallback_config()
            else:
                self._populate_fields(data)
            self._loading_fields = False
            self._loaded = error is None
            self._set_form_sensitive(error is None)
            if error:
                self._show_failure("Settings could not be loaded", error, "load")
            else:
                self._clear_failure("load")
            self._refresh_status()
            GLib.idle_add(self._fit_loaded_content)

        self.client.call("GetInterfaces", interfaces_loaded)

    def _populate_fields(self, conf):
        was_loading = self._loading_fields
        self._loading_fields = True
        try:
            self.config_data = dict(conf)
            self.combo_backend.set_selected(1 if conf.get("BACKEND") == "networkmanager" else 0)
            use_nm = conf.get("BACKEND") == "networkmanager"
            self.combo_inet_iface.set_sensitive(not use_nm)
            self.combo_inet_iface.set_subtitle("Uses the host default route, including Ethernet or VPN"
                if use_nm else "Choose the same Wi-Fi adapter for simultaneous Wi-Fi + hotspot")
            for row, key in ((self.combo_wifi_iface, "WIFI_IFACE"),
                             (self.combo_inet_iface, "INTERNET_IFACE")):
                model = row.get_model()
                if model:
                    for index in range(model.get_n_items()):
                        if model.get_string(index) == conf.get(key):
                            row.set_selected(index)
                            break
            self.entry_ssid.set_text(conf.get("SSID", get_default_ssid()))
            self.entry_pass.set_text(conf.get("PASSPHRASE", "12345678"))
            self.entry_gateway.set_text(conf.get("GATEWAY", "192.168.12.1"))
            self.entry_channel.set_text(conf.get("CHANNEL", "default"))

            self.switch_hidden.set_active(conf.get("HIDDEN", "0") == "1")
            self.switch_isolate.set_active(conf.get("ISOLATE_CLIENTS", "0") == "1")
            self.switch_80211n.set_active(conf.get("IEEE80211N", "0") == "1")
            self.switch_80211ac.set_active(conf.get("IEEE80211AC", "0") == "1")
            self.switch_80211ax.set_active(conf.get("IEEE80211AX", "0") == "1")

            band = conf.get("FREQ_BAND", "auto")
            if band == "2.4":
                self.combo_band.set_selected(1)
            elif band == "5":
                self.combo_band.set_selected(2)
            else:
                self.combo_band.set_selected(0)
        finally:
            self._loading_fields = was_loading

    def _load_fallback_config(self):
        conf_path = "/etc/wifi-hotspot.conf"
        if not os.path.exists(conf_path):
            conf_path = os.path.expanduser("~/.config/wifi-hotspot.conf")
        conf = {}
        try:
            if os.path.exists(conf_path):
                with open(conf_path, "r") as f:
                    for line in f:
                        if "=" in line and not line.startswith("#"):
                            k, v = line.rstrip("\r\n").split("=", 1)
                            conf[k] = v
        except OSError:
            # Credentials are owner-only; keep settings usable if authorization failed.
            conf = {}

        # Local interface scan
        wifi = []
        all_ifaces = []
        try:
            for iface in os.listdir("/sys/class/net"):
                if iface == "lo":
                    continue
                all_ifaces.append(iface)
                if os.path.exists(f"/sys/class/net/{iface}/wireless") or os.path.exists(f"/sys/class/net/{iface}/phy80211"):
                    wifi.append(iface)
        except Exception:
            pass

        self.combo_wifi_iface.set_model(Gtk.StringList.new(wifi or ["wlp2s0"]))
        self.combo_inet_iface.set_model(Gtk.StringList.new(all_ifaces or ["wlp2s0", "enp1s0"]))
        self.row_caps.set_subtitle("Hotspot service unavailable. Check that Wi-Fi Relay is installed and running.")
        self._populate_fields(conf)

    def _refresh_status(self):
        if self._closed:
            return GLib.SOURCE_REMOVE
        if not self.dbus_proxy or self._hotspot_busy or self._status_pending:
            return GLib.SOURCE_CONTINUE
        self._status_pending = True
        revision = self._status_revision
        def status_loaded(status, error):
            self._status_pending = False
            if revision != self._status_revision or self._hotspot_busy:
                return
            if error:
                self.switch_hotspot.set_sensitive(False)
                action = self._retry_action or ("status" if self._loaded else "load")
                self._show_failure("Hotspot service unavailable", error, action)
                return
            self._clear_failure("status")
            self._status_revision += 1
            active = status["active"]
            requested = active or status.get("desired_active", False)
            self.combo_backend.set_sensitive(self._loaded and not requested)
            self.switch_hotspot.set_sensitive(self._loaded or requested)
            self.switch_hotspot.handler_block_by_func(self._on_switch_toggled)
            self.switch_hotspot.set_active(requested)
            self.switch_hotspot.handler_unblock_by_func(self._on_switch_toggled)
            if active and self._retry_action is None:
                self.row_status.set_subtitle(f"On — SSID: {status.get('ssid')} ({status.get('iface')})")
            elif not active:
                if self._retry_action is None:
                    self.row_status.set_subtitle(status.get("error") or ("Waiting for Wi-Fi" if requested else "Off"))
                self._update_clients_list([])
            if active and not self._clients_pending:
                self._clients_pending = True
                client_revision = self._status_revision
                def clients_loaded(clients, error):
                    self._clients_pending = False
                    if not error and client_revision == self._status_revision and not self._hotspot_busy:
                        self._update_clients_list(clients)
                self.client.call("GetClients", clients_loaded)
        self.client.call("GetStatus", status_loaded)
        return GLib.SOURCE_CONTINUE

    def _update_clients_list(self, clients):
        if clients:
            self.row_no_clients.set_title(f"{len(clients)} Device(s) Connected")
            client_texts = []
            for c in clients:
                host = c.get('hostname')
                ip = c.get('ip')
                if host and ip:
                    client_texts.append(f"• {host} ({ip})")
                elif host:
                    client_texts.append(f"• {host}")
                elif ip:
                    client_texts.append(f"• Device ({ip})")
                else:
                    client_texts.append(f"• Device (Connecting...)")
            self.row_no_clients.set_subtitle("\n".join(client_texts))
        else:
            self.row_no_clients.set_title("No devices connected")
            self.row_no_clients.set_subtitle("Connected devices will be listed here")

    def _on_switch_toggled(self, switch, state):
        if not self.dbus_proxy or self._hotspot_busy or self._loading_fields or (state and not self._loaded):
            return True
        config = self._collect_config() if state else None
        if state and config is None:
            return True
        if state:
            if self._save_timer:
                GLib.source_remove(self._save_timer)
                self._save_timer = 0
            self._pending_config = None
        self._hotspot_busy = True
        self._status_revision += 1
        self._set_form_sensitive(False)
        switch.set_sensitive(False)
        self.row_status.set_subtitle("Starting hotspot…" if state else "Stopping hotspot…")
        def operate(saved=None, error=None):
            if error:
                self._pending_config = dict(saved) if saved is not None else None
                self._on_hotspot_finished(None, error, state)
                return
            if saved is not None:
                self.config_data = saved
            self.client.call("Start" if state else "Stop",
                             lambda data, failure: self._on_hotspot_finished(data, failure, state),
                             timeout=60000)
        if state:
            self.writer.submit(config, operate)
        else:
            operate()
        return True

    def _on_hotspot_finished(self, data, error, starting):
        if not error and ((starting and not data.get("success")) or (not starting and not data)):
            error = RuntimeError(data.get("error") or "Failed to start hotspot" if starting
                                 else "Failed to stop hotspot")
        if error:
            self._show_failure("Hotspot could not start" if starting else "Hotspot could not stop",
                               error, "save" if starting and self._pending_config is not None else ("start" if starting else "stop"))
            self.add_toast(str(error))
        else:
            self._clear_failure("start", "stop")
        self._hotspot_busy = False
        self._set_form_sensitive(self._loaded)
        self.switch_hotspot.set_sensitive(self._loaded)
        self._refresh_status()

    def _on_field_changed(self, *args):
        if self._loading_fields or self._hotspot_busy or self._closed or self._closing_after_save:
            return False
        config = self._collect_config()
        if config is None:
            return False
        self._pending_config = config
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(500, self._flush_config)
        return True

    def _flush_config(self):
        self._save_timer = 0
        config, self._pending_config = self._pending_config, None
        if config is not None and not self._closed:
            self.writer.submit(config, self._on_config_saved)
        return GLib.SOURCE_REMOVE

    def _on_config_saved(self, config, error):
        if error:
            if self._pending_config is None:
                self._pending_config = dict(config)
            self._show_failure("Settings were not saved", error, "save")
            return
        self._clear_failure("save")
        self.config_data = config
        if self._hotspot_busy:
            return
        use_nm = config["BACKEND"] == "networkmanager"
        self.combo_inet_iface.set_sensitive(not use_nm)
        self.combo_inet_iface.set_subtitle("Uses the host default route, including Ethernet or VPN"
            if use_nm else "Choose the same Wi-Fi adapter for simultaneous Wi-Fi + hotspot")
        if self._retry_action is None:
            self.row_status.set_title("Hotspot")
            self._refresh_status()

    def _collect_config(self):
        wifi_item = self.combo_wifi_iface.get_selected_item()
        inet_item = self.combo_inet_iface.get_selected_item()
        if wifi_item is None or inet_item is None:
            self.row_status.set_title("Settings were not saved")
            self.row_status.set_subtitle("Select a Wi-Fi adapter and sharing interface first.")
            return None
        # Auto-save the selected interfaces without discarding other options.
        band_idx = self.combo_band.get_selected()
        band_str = "auto" if band_idx == 0 else ("2.4" if band_idx == 1 else "5")

        conf = {
            **self.config_data,
            "BACKEND": "networkmanager" if self.combo_backend.get_selected() == 1 else "create_ap",
            "SSID": self.entry_ssid.get_text() or get_default_ssid(),
            "PASSPHRASE": self.entry_pass.get_text(),
            "GATEWAY": self.entry_gateway.get_text() or "192.168.12.1",
            "CHANNEL": self.entry_channel.get_text() or "default",
            "FREQ_BAND": band_str,
            "HIDDEN": "1" if self.switch_hidden.get_active() else "0",
            "ISOLATE_CLIENTS": "1" if self.switch_isolate.get_active() else "0",
            "IEEE80211N": "1" if self.switch_80211n.get_active() else "0",
            "IEEE80211AC": "1" if self.switch_80211ac.get_active() else "0",
            "IEEE80211AX": "1" if self.switch_80211ax.get_active() else "0",
            "WIFI_IFACE": wifi_item.get_string(),
            "INTERNET_IFACE": inet_item.get_string(),
        }

        return conf

    def _on_show_qr(self, button):
        if not self._loaded:
            self.add_toast("Wait for the hotspot settings to load before sharing.")
            return
        ssid = self.entry_ssid.get_text()
        pwd = self.entry_pass.get_text()
        try:
            payload = wifi_payload(ssid, pwd, self.switch_hidden.get_active())
            width, pixels = qr_rgb(payload)
        except ValueError as error:
            self.add_toast(str(error))
            return
        texture = Gdk.MemoryTexture.new(width, width, Gdk.MemoryFormat.R8G8B8,
                                        GLib.Bytes.new(pixels), width * 3)
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_alternative_text("Wi-Fi connection QR code for " + ssid)
        picture.set_can_shrink(True)
        picture.set_size_request(width, width)
        picture.set_halign(Gtk.Align.CENTER)
        dialog = MessageDialog(
            transient_for=self,
            heading="Wi-Fi Connection Code",
            body=f"Network Name: {ssid}\nPassword: {pwd}\n\nScan the QR code with your phone’s camera to connect.",
        )
        dialog.set_extra_child(picture)
        dialog.add_response("ok", "Close")
        dialog.present()
        return dialog


class HotspotSettingsApp(HotspotApplication):
    def __init__(self):
        super().__init__(
            application_id="io.github.erhanzeyrek.WifiHotspot",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = HotspotSettingsWindow(self)
        win.present()


class HotspotAboutApp(HotspotApplication):
    """A separate window that needs neither configuration nor service access."""
    def __init__(self):
        super().__init__(application_id=BUS_NAME + ".About",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            if not GNOME_DESKTOP:
                win = Gtk.AboutDialog(application=self, program_name="Wi-Fi Relay", version="1.0.0",
                    logo_icon_name=BUS_NAME, authors=["Erhan Zeyrek", "Wi-Fi Relay contributors"],
                    copyright="© 2026 Wi-Fi Relay contributors", license_type=Gtk.License.MIT_X11,
                    website="https://github.com/3togo/gnome-wifi-hotspot",
                    comments="Share your internet connection with optional desktop controls.")
                win.add_credit_section("Third-party components", [
                    "create_ap — oblique and lakinduaksh (BSD-2-Clause)",
                    "QR Code generator — Project Nayuki (MIT)"])
                win.present()
                return
            win = Adw.AboutWindow(
                application=self,
                application_name="Wi-Fi Relay",
                application_icon=BUS_NAME,
                version="1.0.0",
                developer_name="Erhan Zeyrek and Wi-Fi Relay contributors",
                developers=["Erhan Zeyrek", "Wi-Fi Relay contributors"],
                copyright="© 2026 Wi-Fi Relay contributors",
                license_type=Gtk.License.MIT_X11,
                website="https://github.com/3togo/gnome-wifi-hotspot",
                issue_url="https://github.com/3togo/gnome-wifi-hotspot/issues",
                comments="Share your internet connection with optional desktop controls.",
            )
            win.add_acknowledgement_section("Third-party components", [
                "create_ap — oblique and lakinduaksh (BSD-2-Clause)",
                "QR Code generator — Project Nayuki (MIT)",
            ])
        win.present()


if __name__ == "__main__":
    app = HotspotSettingsApp()
    sys.exit(app.run(sys.argv))
