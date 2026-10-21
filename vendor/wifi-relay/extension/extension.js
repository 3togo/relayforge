import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension, gettext as _ } from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.erhanzeyrek.WifiHotspot';
const OBJECT_PATH = '/io/github/erhanzeyrek/WifiHotspot';
const INTERFACE_NAME = 'io.github.erhanzeyrek.WifiHotspot';

const HotspotDBusInterface = `
<node>
  <interface name="${INTERFACE_NAME}">
    <method name="Start">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="Stop">
      <arg type="b" name="success" direction="out"/>
    </method>
    <method name="GetStatus">
      <arg type="s" name="status_json" direction="out"/>
    </method>
    <method name="GetClients">
      <arg type="s" name="clients_json" direction="out"/>
    </method>
    <method name="GetConfig">
      <arg type="s" name="config_json" direction="out"/>
    </method>
    <method name="SetConfig">
      <arg type="s" name="config_json" direction="in"/>
      <arg type="b" name="success" direction="out"/>
    </method>
    <method name="SwitchBandAndReconnect">
      <arg type="s" name="target_band" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <signal name="StatusChanged">
      <arg type="s" name="status_json"/>
    </signal>
    <signal name="ClientsChanged">
      <arg type="s" name="clients_json"/>
    </signal>
    <signal name="UserActionRequired">
      <arg type="s" name="action_json"/>
    </signal>
  </interface>
</node>`;

const HotspotProxy = Gio.DBusProxy.makeProxyWrapper(HotspotDBusInterface);

const HotspotToggle = GObject.registerClass(
class HotspotToggle extends QuickSettings.QuickMenuToggle {
    _init(extension) {
        super._init({
            title: _('Wi-Fi Relay'),
            subtitle: _('Off'),
            iconName: 'network-wireless-hotspot-symbolic',
            toggleMode: true,
        });

        this._extension = extension;
        this._isBusy = false;
        this._statusRevision = 0;
        this._clients = [];
        this._status = { active: false, ssid: '', client_count: 0 };

        // 1. Configure Header & Menu
        this.menu.setHeader('network-wireless-hotspot-symbolic', _('Wi-Fi Relay'), this.subtitle);

        // 2. Dynamic Details Section
        this._detailsSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._detailsSection);

        // 3. Separator & Hotspot Settings Link
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction(_('Hotspot Settings'), () => {
            this._openSettings();
        });

        this.menu.addAction(_('About'), () => this._openAbout());

        // 4. Primary Button Click Event
        this.connect('clicked', () => this._onToggleClicked());

        // 5. Connect D-Bus Proxy
        this._initDBus();
    }

    _initDBus() {
        try {
            this._proxy = new HotspotProxy(
                Gio.DBus.system,
                BUS_NAME,
                OBJECT_PATH,
                (proxy, error) => {
                    if (error) {
                        console.warn(`[Hotspot] D-Bus proxy error: ${error.message}`);
                        return;
                    }
                    if (!this._destroyed) this._onProxyReady();
                }
            );
        } catch (e) {
            console.error(`[Hotspot] Failed to create D-Bus proxy: ${e}`);
        }
    }

    _onProxyReady() {
        // Allow time for scanning, reconnecting, and hotspot startup.
        this._proxy.set_default_timeout(60000);
        // Listen to signals
        this._statusSignalId = this._proxy.connectSignal('StatusChanged', (_proxy, _sender, [statusJson]) => {
            try {
                const status = JSON.parse(statusJson);
                this._updateUI(status);
            } catch (e) {
                console.error(`[Hotspot] StatusChanged parse error: ${e}`);
            }
        });

        this._clientsSignalId = this._proxy.connectSignal('ClientsChanged', (_proxy, _sender, [clientsJson]) => {
            try {
                this._clients = JSON.parse(clientsJson);
                this._renderMenuDetails();
                this._syncTray();
            } catch (e) {
                console.error(`[Hotspot] ClientsChanged parse error: ${e}`);
            }
        });

        this._actionSignalId = this._proxy.connectSignal('UserActionRequired', (_proxy, _sender, [actionJson]) => {
            try {
                const action = JSON.parse(actionJson);
                this._handleUserActionRequired(action);
            } catch (e) {
                console.error(`[Hotspot] UserActionRequired parse error: ${e}`);
            }
        });

        // Initial status query
        this._queryStatus();

        // Polling fallback every 5s
        this._pollTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            this._queryStatus();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _queryStatus() {
        if (!this._proxy || this._isBusy) return;
        const revision = this._statusRevision;
        this._proxy.GetStatusRemote((result, error) => {
            if (error || !this._proxy || revision !== this._statusRevision) return;
            try {
                const [statusJson] = result;
                const status = JSON.parse(statusJson);
                this._updateUI(status);
            } catch (e) {}
        });
    }

    _updateUI(status) {
        this._statusRevision++;
        this._status = status;
        const active = !!status.active;
        const transitioning = ['connecting', 'stopping'].includes(status.state) || this._isBusy;
        this.checked = active || !!status.desired_active;
        this.reactive = !this._isBusy && status.state !== 'stopping' &&
            (status.state !== 'connecting' || !!status.desired_active);

        if (transitioning) {
            this.subtitle = status.state === 'stopping' ? _('Stopping...') : _('Connecting...');
            this.menu.setHeader('network-wireless-hotspot-symbolic', _('Wi-Fi Relay'), this.subtitle);
        } else if (active) {
            const count = status.client_count || this._clients.length;
            this.subtitle = count > 0 ? `${status.ssid} (${count} devices)` : (status.ssid || _('On'));
            this.menu.setHeader('network-wireless-hotspot-symbolic', status.ssid || _('Wi-Fi Relay'), _('Active'));
        } else {
            this.subtitle = status.desired_active ? _('Waiting for Wi-Fi') : _('Off');
            this.menu.setHeader('network-wireless-hotspot-symbolic', _('Wi-Fi Relay'), this.subtitle);
        }

        this._renderMenuDetails();
        this._syncTray();
    }

    _syncTray() {
        this._extension.tray?.update(this._status, this._clients, this._isBusy);
    }

    _setBusy(state) {
        this._isBusy = true;
        this._updateUI({...this._status, state});
    }

    _finishOperation() {
        if (!this._proxy) return;
        this._isBusy = false;
        this._updateUI({...this._status, state: this._status.active ? 'on' : 'off'});
        this._queryStatus();
    }

    _renderMenuDetails() {
        this._detailsSection.removeAll();

        if (this._status.active) {
            const bandText = (this._status.band && this._status.band !== 'auto') ? `${this._status.band} GHz` : '2.4 GHz';
            const ssidItem = new PopupMenu.PopupImageMenuItem(
                `SSID: ${this._status.ssid || 'Hotspot'} (${bandText})`,
                'network-wireless-hotspot-symbolic'
            );
            ssidItem.reactive = false;
            this._detailsSection.addMenuItem(ssidItem);

            const countItem = new PopupMenu.PopupMenuItem(
                `Connected Devices: ${this._clients.length}`
            );
            countItem.reactive = false;
            this._detailsSection.addMenuItem(countItem);

            for (const client of this._clients) {
                let label = '';
                if (client.hostname && client.ip) {
                    label = `${client.hostname} (${client.ip})`;
                } else if (client.hostname) {
                    label = client.hostname;
                } else if (client.ip) {
                    label = `Device (${client.ip})`;
                } else {
                    label = `Device (${_('Connecting...')})`;
                }
                const clientRow = new PopupMenu.PopupImageMenuItem(label, 'computer-symbolic');
                clientRow.reactive = false;
                this._detailsSection.addMenuItem(clientRow);
            }
        } else {
            const item = new PopupMenu.PopupMenuItem(_('Hotspot is currently turned off.'));
            item.reactive = false;
            this._detailsSection.addMenuItem(item);
        }
    }

    _onToggleClicked() {
        if (!this._proxy || this._isBusy || this._status.state === 'stopping' ||
            (this._status.state === 'connecting' && !this._status.desired_active)) return;
        this._setBusy((this._status.active || this._status.desired_active) ? 'stopping' : 'connecting');

        const isCurrentlyActive = !!(this._status.active || this._status.desired_active);

        if (isCurrentlyActive) {
            // Currently active -> Turn OFF
            this.subtitle = _('Stopping...');
            this._proxy.StopRemote((result, error) => {
                if (!this._proxy) return;
                this._finishOperation();
                if (error) {
                    console.error(`[Hotspot] Stop error: ${error.message}`);
                    Main.notify(_('Wi-Fi Relay'), _('Stop error: ') + error.message);
                }
            });
        } else {
            // Currently inactive -> Turn ON
            this._proxy.StartRemote((result, error) => {
                if (!this._proxy) return;
                this._finishOperation();
                if (error) {
                    console.error(`[Hotspot] Start error: ${error.message}`);
                    let userMsg = error.message;
                    if (error.message.includes('ServiceUnknown') || error.message.includes('not activatable')) {
                        userMsg = _('Background service (daemon) is not running.');
                    }
                    Main.notify(_('Wi-Fi Relay'), _('Start error: ') + userMsg);
                    this.checked = false;
                    this.subtitle = _('Off');
                    return;
                }

                try {
                    const [resJson] = result;
                    const res = JSON.parse(resJson);
                    if (!res.success && res.action_required === 'band_switch') {
                        this._handleUserActionRequired(res);
                    } else if (!res.success) {
                        Main.notify(_('Wi-Fi Relay'), _('Start error: ') + (res.error || _('Failed to start Hotspot.')));
                        this.checked = false;
                        this.subtitle = _('Off');
                        return;
                    }
                } catch (e) {}
            });
        }
    }

    _handleUserActionRequired(action) {
        if (action.type === 'error') {
            Main.notify(_('Wi-Fi Relay'), action.message || _('Hotspot stopped unexpectedly.'));
            this._queryStatus();
            return;
        }
        if (action.type === 'band_switch' || action.action_required === 'band_switch') {
            const msg = action.message || _("Your Wi-Fi adapter does not support 5GHz Hotspot. Switch to 2.4GHz and start?");
            
            // Show system notification with quick action button
            const source = new MessageTray.Source({
                title: _('Wi-Fi Relay'),
                iconName: 'network-wireless-hotspot-symbolic',
            });
            Main.messageTray.add(source);

            const notification = new MessageTray.Notification({
                source: source,
                title: _('Band Compatibility Warning'),
                body: msg,
                isTransient: false,
            });

            notification.addAction(_("Switch to 2.4GHz and Start"), () => {
                if (!this._proxy || this._isBusy) return;
                this._setBusy('connecting');
                this._proxy.SwitchBandAndReconnectRemote('2.4', (res, err) => {
                    if (!this._proxy) return;
                    this._finishOperation();
                    if (err) {
                        Main.notify(_('Wi-Fi Relay'), _('Band switch error: ') + err.message);
                        return;
                    }
                    try {
                        const response = JSON.parse(res[0]);
                        if (!response.success) {
                            Main.notify(_('Wi-Fi Relay'), _('Band switch error: ') + response.error);
                            this._queryStatus();
                            return;
                        }
                    } catch (e) {
                        Main.notify(_('Wi-Fi Relay'), _('Invalid band switch response.'));
                        return;
                    }
                    // Start after the verified reconnection
                    this._restartTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
                        this._restartTimer = null;
                        this._onToggleClicked();
                        return GLib.SOURCE_REMOVE;
                    });
                });
            });

            source.showNotification(notification);
        }
    }

    _openSettings() {
        this._launchWindow([], _('Could not open network settings: '));
    }

    _openAbout() {
        this._launchWindow(['--about'], _('Could not open About: '));
    }

    _launchWindow(arguments_, errorMessage) {
        try {
            Gio.Subprocess.new(['wifi-hotspot-settings', ...arguments_], Gio.SubprocessFlags.NONE);
        } catch (error) {
            Main.notify(_('Wi-Fi Relay'), errorMessage + error.message);
        }
    }

    destroy() {
        this._destroyed = true;
        if (this._restartTimer) {
            GLib.source_remove(this._restartTimer);
            this._restartTimer = null;
        }
        if (this._pollTimer) {
            GLib.source_remove(this._pollTimer);
            this._pollTimer = null;
        }
        if (this._proxy) {
            if (this._statusSignalId) this._proxy.disconnectSignal(this._statusSignalId);
            if (this._clientsSignalId) this._proxy.disconnectSignal(this._clientsSignalId);
            if (this._actionSignalId) this._proxy.disconnectSignal(this._actionSignalId);
            this._proxy = null;
        }
        super.destroy();
    }
});

// A dedicated panel button keeps the hotspot visible even when it is off.
const HotspotTray = GObject.registerClass(
class HotspotTray extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, _('Wi-Fi Relay'));
        this._icon = new St.Icon({
            icon_name: 'network-wireless-hotspot-symbolic',
            style_class: 'system-status-icon hotspot-tray-off',
        });
        this.add_child(this._icon);
        this._statusItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._statusItem);
        this._switch = new PopupMenu.PopupSwitchMenuItem(_('Wi-Fi Relay'), false);
        this._switch.connect('toggled', () => extension._indicator?._toggle._onToggleClicked());
        this.menu.addMenuItem(this._switch);
        this._details = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._details);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction(_('Hotspot Settings'), () => extension._indicator?._toggle._openSettings());
        this.menu.addAction(_('About'), () => extension._indicator?._toggle._openAbout());
        this.update({active: false}, [], false);
    }

    update(status, clients, busy) {
        const stopping = status.state === 'stopping';
        const transitioning = busy || stopping || status.state === 'connecting';
        const state = transitioning || (status.desired_active && !status.active)
            ? 'connecting' : status.active ? 'on' : 'off';
        const label = transitioning
            ? (stopping ? _('Stopping...') : _('Connecting...'))
            : status.active ? _('On') : status.desired_active ? _('Waiting for Wi-Fi') : _('Off');
        this._icon.style_class = `system-status-icon hotspot-tray-${state}`;
        this.accessible_name = `${_('Wi-Fi Relay')}: ${label}`;
        this._statusItem.label.text = this.accessible_name;
        this._switch.setToggleState(!!(status.active || status.desired_active));
        this._switch.setSensitive(!busy && !stopping &&
            (status.state !== 'connecting' || !!status.desired_active));
        this._details.removeAll();
        if (status.active) {
            this._details.addMenuItem(new PopupMenu.PopupMenuItem(
                `SSID: ${status.ssid || 'Hotspot'}`, {reactive: false}));
            this._details.addMenuItem(new PopupMenu.PopupMenuItem(
                `${_('Connected Devices')}: ${status.client_count ?? clients.length}`, {reactive: false}));
            for (const client of clients) {
                const name = client.hostname || _('Device');
                this._details.addMenuItem(new PopupMenu.PopupMenuItem(
                    client.ip ? `${name} (${client.ip})` : name, {reactive: false}));
            }
        }
    }
});

const HotspotIndicator = GObject.registerClass(
class HotspotIndicator extends QuickSettings.SystemIndicator {
    _init(extension) {
        super._init();
        this._toggle = new HotspotToggle(extension);
        this.quickSettingsItems.push(this._toggle);
    }

    destroy() {
        this._toggle?.destroy();
        this._toggle = null;
        super.destroy();
    }
});

export default class WifiHotspotExtension extends Extension {
    enable() {
        this.tray = new HotspotTray(this);
        Main.panel.addToStatusArea(this.uuid, this.tray);
        this._indicator = new HotspotIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
        this.tray?.destroy();
        this.tray = null;
    }
}
