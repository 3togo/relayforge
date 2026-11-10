// Test-only observer, loaded only in the disposable guest's Shell session.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

export default class ValidationProbe extends Extension {
    enable() {
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            const windows = global.get_window_actors().map(actor => {
                const window = actor.meta_window;
                return {pid: window.get_pid(), title: window.get_title(),
                    app: window.get_wm_class(), visible: actor.visible};
            });
            Gio.File.new_for_path('/home/ubuntu/results/windows.json').replace_contents(
                JSON.stringify(windows), null, false,
                Gio.FileCreateFlags.REPLACE_DESTINATION, null);
            return GLib.SOURCE_CONTINUE;
        });
    }
    disable() {
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
    }
}
