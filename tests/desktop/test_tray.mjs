// Exercise the extension's tray/controller without changing host networking.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Menu {
    items = [];
    addMenuItem(item) { this.items.push(item); }
    addAction(label, action) { this.items.push({label, action}); }
    setHeader() {}
    removeAll() { this.items = []; }
}
class Actor {
    constructor(...args) { this._init(...args); }
    _init() { this.menu = new Menu(); this.quickSettingsItems = []; }
    connect() {}
    add_child() {}
    destroy() {}
}
class Item {
    constructor(text) { this.label = {text}; }
    connect() {}
    setToggleState(value) { this.state = value; }
    setSensitive(value) { this.sensitive = value; }
}
let query, start, stop;
const launches = [];
const proxy = {
    GetStatusRemote(callback) { query = callback; },
    StartRemote(callback) { start = callback; },
    StopRemote(callback) { stop = callback; },
    disconnectSignal() {},
};
const context = {
    GObject: {registerClass: cls => cls},
    Gio: {Subprocess: {new: args => launches.push(Array.from(args))}, SubprocessFlags: {NONE: 0}, DBus: {system: {}}, DBusProxy: {makeProxyWrapper: () => class {constructor() {return proxy;}}}},
    GLib: {source_remove() {}},
    St: {Icon: class {constructor(props) {Object.assign(this, props);}}},
    PanelMenu: {Button: Actor},
    QuickSettings: {QuickMenuToggle: Actor, SystemIndicator: Actor},
    PopupMenu: {PopupMenuItem: Item, PopupImageMenuItem: Item, PopupSwitchMenuItem: Item,
        PopupMenuSection: Menu, PopupSeparatorMenuItem: Item},
    Main: {notify() {}}, MessageTray: {}, Extension: class {}, _: text => text,
    console: {warn() {}, error() {}},
};
const source = readFileSync(new URL('../../desktop/extension.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export default class', 'class');
vm.runInNewContext(`${source}\nglobalThis.classes = {HotspotTray, HotspotToggle};`, context);
const extension = {};
extension.tray = new context.classes.HotspotTray(extension);
const toggle = new context.classes.HotspotToggle(extension);
const tray = extension.tray;
assert.match(tray._icon.style_class, /hotspot-tray-off/);
assert.equal(tray._switch.sensitive, true);

extension._indicator = {_toggle: toggle};
for (const menu of [tray.menu, toggle.menu]) {
    menu.items.find(item => item.label === 'About').action();
}
assert.deepEqual(launches, [['wifi-hotspot-settings', '--about'], ['wifi-hotspot-settings', '--about']]);
assert.equal(start, undefined);
assert.equal(stop, undefined);

toggle._onToggleClicked();
assert.match(tray._icon.style_class, /hotspot-tray-connecting/);
assert.equal(tray._switch.sensitive, false);
start(['{"success":false,"error":"No permitted channel"}'], null);
query(['{"active":false}'], null);
assert.match(tray._icon.style_class, /hotspot-tray-off/);
assert.equal(tray._switch.sensitive, true);

// A reply from a query made before a signal must not overwrite that signal.
toggle._queryStatus();
const staleQuery = query;
toggle._updateUI({active: false, state: 'connecting'});
staleQuery(['{"active":false}'], null);
assert.match(tray._icon.style_class, /hotspot-tray-connecting/);

toggle._updateUI({active: true, ssid: 'Test', client_count: 1});
assert.match(tray._icon.style_class, /hotspot-tray-on/);
assert.equal(tray._switch.state, true);
toggle._onToggleClicked();
assert.match(tray._icon.style_class, /hotspot-tray-connecting/);
assert.match(tray.accessible_name, /Stopping/);
toggle._updateUI({active: false});
stop([true], null);
query(['{"active":false}'], null);
assert.match(tray._icon.style_class, /hotspot-tray-off/);

// Pending recovery stays checked and can be explicitly cancelled.
toggle._updateUI({active: false, desired_active: true, state: 'waiting'});
assert.equal(toggle.checked, true);
assert.equal(tray._switch.state, true);
assert.equal(tray._switch.sensitive, true);
assert.match(tray.accessible_name, /Waiting for Wi-Fi/);
assert.match(tray._icon.style_class, /hotspot-tray-connecting/);
toggle._updateUI({active: false, desired_active: true, state: 'connecting'});
assert.equal(tray._switch.sensitive, true);
assert.equal(toggle.reactive, true);
toggle._onToggleClicked();
assert.equal(typeof stop, 'function');
stop([true], null);
query(['{"active":false,"desired_active":false}'], null);
assert.equal(toggle.checked, false);

// A saved service failure remains visible in both GNOME entry points.
const channelError = 'Concurrent sharing is not permitted on the current upstream channel';
toggle._updateUI({active: false, state: 'failed', error: channelError});
assert.equal(toggle.subtitle, 'Could not start');
assert.match(tray.accessible_name, /Could not start/);
for (const menu of [toggle._detailsSection, tray._details]) {
    assert.ok(menu.items.some(item => item.label.text === channelError));
    assert.ok(menu.items.some(item => item.label.text.includes('2.4 GHz')));
}
toggle._updateUI({active: false, state: 'failed', error: channelError});
assert.ok(tray._details.items.some(item => item.label.text === channelError));
toggle._updateUI({active: false});
assert.ok(!tray._details.items.some(item => item.label.text === channelError));

// Service loss explains why controls are disabled and a later good poll restores them.
toggle._queryStatus();
query(null, {message: 'ServiceUnknown'});
assert.equal(toggle.subtitle, 'Service unavailable');
assert.equal(toggle.reactive, false);
assert.equal(tray._switch.sensitive, false);
assert.ok(tray._details.items.some(item => item.label.text.includes('Hotspot service unavailable')));
toggle._queryStatus();
query(['{"active":false}'], null);
assert.equal(toggle.reactive, true);
assert.equal(tray._switch.sensitive, true);

// Pending callbacks after disable must not touch destroyed actors.
toggle._onToggleClicked();
const pendingStart = start;
toggle.destroy();
pendingStart(['{"success":true}'], null);
console.log('Tray state, failed startup, stale replies, and disable checks passed.');
