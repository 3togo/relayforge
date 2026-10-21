import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';
import { ExtensionPreferences, gettext as _ } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class HotspotPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const page = new Adw.PreferencesPage({title: _('Wi-Fi Relay'), icon_name: 'network-wireless-hotspot-symbolic'});
        const group = new Adw.PreferencesGroup({
            title: _('Network settings'),
            description: _('Configure sharing, startup, and connected devices in network settings.'),
        });
        const row = new Adw.ActionRow({title: _('Open network settings')});
        const button = new Gtk.Button({label: _('Open'), valign: Gtk.Align.CENTER});
        button.connect('clicked', () => {
            try {
                Gio.Subprocess.new(['wifi-hotspot-settings'], Gio.SubprocessFlags.NONE);
            } catch (error) {
                group.set_description(_('Could not open network settings: ') + error.message);
            }
        });
        row.add_suffix(button);
        row.set_activatable_widget(button);
        group.add(row);
        page.add(group);
        window.add(page);
    }
}
