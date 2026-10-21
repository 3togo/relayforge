#!/usr/bin/python3
"""StatusNotifier tray for desktops that do not run GNOME Shell."""
import json
import os
from pathlib import Path
import sys

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, Gio, GLib, AyatanaAppIndicator3 as AppIndicator

from startup import get_auto_start
from visibility import get_tray_visible
from lifecycle import CodeRevision, replace_tray_instance
from service_client import decode_reply

BUS_NAME = 'io.github.erhanzeyrek.WifiHotspot'
OBJECT_PATH = '/io/github/erhanzeyrek/WifiHotspot'


class HotspotTray(Gtk.Application):
    def __init__(self, session_start=False):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.ALLOW_REPLACEMENT
        if session_start:
            flags |= Gio.ApplicationFlags.REPLACE
        super().__init__(application_id=BUS_NAME + '.Tray', flags=flags)
        self.add_main_option('show-icon', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Show the tray icon in this session', None)
        self.add_main_option('session-start', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Refresh tray controls after package installation', None)
        self.manual_start = False
        self.proxy = None
        self.indicator = None
        self.status = {'active': False}
        self.clients = []
        self.busy = False
        self.revision = 0
        self.query_pending = False
        self.closed = False
        self.cancel = Gio.Cancellable()
        self.poll_timer = 0
        self.restarting = False
        directory = Path(__file__).resolve().parent
        self.code_revision = CodeRevision(directory / name for name in
            ('tray.py', 'startup.py', 'visibility.py', 'preferences.py', 'lifecycle.py', 'service_client.py'))

    def do_command_line(self, command_line):
        if command_line.get_options_dict().contains('show-icon'):
            self.manual_start = True
        self.activate()
        return 0

    def _update_visibility(self):
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE if get_tray_visible()
                                  else AppIndicator.IndicatorStatus.PASSIVE)

    def do_activate(self):
        if self.indicator:
            self._update_visibility()
            return
        if not get_auto_start() and not self.manual_start:
            self.quit()
            return
        self.hold()
        icon_dir = Path(__file__).resolve().parent / 'icons'
        self.indicator = AppIndicator.Indicator.new_with_path(
            'wifi-hotspot', 'wifi-hotspot-off',
            AppIndicator.IndicatorCategory.HARDWARE, str(icon_dir))
        self.indicator.set_title('Wi-Fi Relay')
        self._render()
        self._update_visibility()
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
            BUS_NAME, OBJECT_PATH, BUS_NAME, self.cancel, self._proxy_ready)
        self.poll_timer = GLib.timeout_add_seconds(3, self._poll)

    def _proxy_ready(self, _source, result):
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
            if self.closed:
                return
            self.proxy.set_default_timeout(60000)
            self.proxy.connect('g-signal', self._signal)
            self.proxy.connect('notify::g-name-owner', self._owner_changed)
            self._query()
        except GLib.Error as error:
            if not self.closed:
                self._error(str(error))

    def _owner_changed(self, *_args):
        self.revision += 1
        self._query()

    def _signal(self, _proxy, _sender, name, parameters):
        try:
            if self.closed or name not in ('StatusChanged', 'ClientsChanged'):
                return
            data = decode_reply('GetStatus' if name == 'StatusChanged' else 'GetClients', parameters)
            if name == 'StatusChanged':
                self.status = data
                self.revision += 1
            elif name == 'ClientsChanged':
                self.clients = data
                self.status['client_count'] = len(data)
            else:
                return
            self._render()
        except (ValueError, TypeError, IndexError):
            pass

    def _poll(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.code_revision.removed():
            self.quit()
            return GLib.SOURCE_REMOVE
        if self.code_revision.changed() and not self.busy and not self.query_pending:
            if not self.restarting:
                self.restarting = True
                GLib.idle_add(self._restart_after_upgrade)
            return GLib.SOURCE_CONTINUE
        if not get_auto_start() and not self.manual_start:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
            self.quit()
            return GLib.SOURCE_REMOVE
        self._update_visibility()
        self._query()
        return GLib.SOURCE_CONTINUE

    def _close(self):
        if self.closed:
            return
        self.closed = True
        if self.poll_timer:
            GLib.source_remove(self.poll_timer)
            self.poll_timer = 0
        self.cancel.cancel()
        if self.proxy:
            self.proxy.disconnect_by_func(self._signal)
            self.proxy.disconnect_by_func(self._owner_changed)

    def do_shutdown(self):
        self._close()
        Gtk.Application.do_shutdown(self)

    def _restart_after_upgrade(self):
        arguments = list(sys.argv)
        if self.manual_start and '--show-icon' not in arguments:
            arguments.append('--show-icon')
        self._close()
        try:
            os.execv(sys.executable, [sys.executable, *arguments])
        except OSError as error:
            print(f'Wi-Fi Relay tray could not reload after upgrade: {error}', file=sys.stderr)
            self.quit()
        return GLib.SOURCE_REMOVE

    def _query(self):
        if not self.proxy or self.busy or self.query_pending:
            return
        revision = self.revision
        self.query_pending = True
        client_revision = None
        def finished(proxy, result):
            nonlocal client_revision
            self.query_pending = False
            try:
                status = decode_reply('GetStatus', proxy.call_finish(result))
                if self.closed or revision != self.revision:
                    return
                self.status = status
                self.revision += 1
                self._render()
                if status.get('active'):
                    client_revision = self.revision
                    self.proxy.call('GetClients', None, Gio.DBusCallFlags.NONE,
                                    10000, self.cancel, clients_finished)
            except (GLib.Error, ValueError, TypeError):
                if not self.closed and revision == self.revision:
                    self.status = {'active': False, 'unavailable': True}
                    self._render()
        def clients_finished(proxy, result):
            try:
                clients = decode_reply('GetClients', proxy.call_finish(result))
                if (self.closed or not self.status.get('active') or self.busy
                        or client_revision != self.revision):
                    return
                self.clients = clients
                self.status['client_count'] = len(self.clients)
                self._render()
            except (GLib.Error, ValueError, TypeError):
                pass
        self.proxy.call('GetStatus', None, Gio.DBusCallFlags.NONE,
                        10000, self.cancel, finished)

    def _render(self):
        stopping = self.status.get('state') == 'stopping'
        transition = self.busy or self.status.get('state') in ('connecting', 'stopping')
        active = bool(self.status.get('active'))
        requested = active or bool(self.status.get('desired_active'))
        state = 'connecting' if transition or (requested and not active) else 'on' if active else 'off'
        label = ('Stopping…' if stopping else 'Connecting…') if transition else 'On' if active else 'Waiting for Wi-Fi' if requested else 'Off'
        self.indicator.set_icon_full('wifi-hotspot-' + state, 'Wi-Fi Relay: ' + label)
        menu = Gtk.Menu()
        status_item = Gtk.MenuItem(label='Wi-Fi Relay: ' + label)
        status_item.set_sensitive(False)
        menu.append(status_item)
        switch = Gtk.CheckMenuItem(label='Enable hotspot')
        switch.set_active(requested)
        switch.set_sensitive(not self.busy and not stopping and
                             (self.status.get('state') != 'connecting' or requested) and self.proxy is not None)
        switch.connect('toggled', self._toggle)
        menu.append(switch)
        if self.status.get('unavailable'):
            row = Gtk.MenuItem(label='Hotspot service unavailable')
            row.set_sensitive(False)
            menu.append(row)
        if active:
            for text in ['SSID: ' + self.status.get('ssid', 'Hotspot'),
                         f"Connected devices: {self.status.get('client_count', len(self.clients))}"]:
                row = Gtk.MenuItem(label=text)
                row.set_sensitive(False)
                menu.append(row)
            for client in self.clients:
                text = client.get('hostname') or 'Device'
                if client.get('ip'):
                    text += f" ({client['ip']})"
                row = Gtk.MenuItem(label=text)
                row.set_sensitive(False)
                menu.append(row)
        menu.append(Gtk.SeparatorMenuItem())
        settings = Gtk.MenuItem(label='Hotspot Settings')
        settings.connect('activate', self._open_settings)
        menu.append(settings)
        about = Gtk.MenuItem(label='About')
        about.connect('activate', self._open_about)
        menu.append(about)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label='Quit')
        quit_item.set_tooltip_text('Stop the hotspot and close the tray')
        quit_item.set_sensitive(not self.busy and self.proxy is not None)
        quit_item.connect('activate', self._quit_hotspot)
        menu.append(quit_item)
        menu.show_all()
        self.menu = menu
        self.indicator.set_menu(menu)

    def _toggle(self, _item):
        if (self.busy or not self.proxy or self.status.get('state') == 'stopping' or
                (self.status.get('state') == 'connecting' and not self.status.get('desired_active'))):
            return
        method = 'Stop' if (self.status.get('active') or self.status.get('desired_active')) else 'Start'
        self._request_change(method)

    def _quit_hotspot(self, _item):
        if self.busy or self.proxy is None or self.closed:
            return
        self._request_change('Stop', quit_after_stop=True)

    def _request_change(self, method, quit_after_stop=False):
        self.busy = True
        self.status['state'] = 'stopping' if method == 'Stop' else 'connecting'
        self.revision += 1
        self._render()
        def finished(proxy, result):
            if self.closed:
                return
            self.busy = False
            self.status.pop('state', None)
            try:
                value = decode_reply(method, proxy.call_finish(result))
                if method == 'Start':
                    response = value
                    if not response.get('success'):
                        self._error(response.get('error', 'Hotspot failed to start.'))
                    elif response.get('status'):
                        self.status = response['status']
                elif not value:
                    self._error('Hotspot could not be stopped.')
                elif quit_after_stop:
                    self.quit()
                    return
            except (GLib.Error, ValueError) as error:
                self._error(str(error))
            self.revision += 1
            self._render()
            self._query()
        self.proxy.call(method, None, Gio.DBusCallFlags.NONE, 60000, self.cancel, finished)

    def _open_settings(self, _item):
        self._launch_window()

    def _open_about(self, _item):
        self._launch_window('--about')

    def _launch_window(self, *arguments):
        try:
            Gio.Subprocess.new(['/usr/bin/python3', str(Path(__file__).resolve().parent / 'launcher.py'), *arguments],
                               Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self._error(str(error))

    def _error(self, message):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text='Wi-Fi Relay')
        dialog.format_secondary_text(message)
        dialog.connect('response', lambda widget, _response: widget.destroy())
        dialog.show_all()


if __name__ == '__main__':
    session_start = '--session-start' in sys.argv
    if session_start:
        if not get_auto_start():
            sys.exit(0)
        try:
            replace_tray_instance(BUS_NAME + '.Tray', Path(__file__).resolve())
        except (OSError, GLib.Error) as error:
            print(f'Wi-Fi Relay tray could not take over: {error}', file=sys.stderr)
            sys.exit(1)
    sys.exit(HotspotTray(session_start=session_start).run(sys.argv))
