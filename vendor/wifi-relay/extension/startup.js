import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const UUID = 'wifi-relay@3togo.github.io';
const LEGACY_UUID = 'wifi-hotspot@erhanzeyrek';
const preferencePath = () => GLib.build_filenamev([
    GLib.get_user_config_dir(), 'wifi-hotspot', 'startup.json',
]);

export function getAutoStart() {
    try {
        const [ok, contents] = GLib.file_get_contents(preferencePath());
        if (ok) return JSON.parse(new TextDecoder().decode(contents)).auto_start !== false;
    } catch (_) {}
    return true;
}

export function setAutoStart(enabled) {
    const settings = new Gio.Settings({schema_id: 'org.gnome.shell'});
    const current = settings.get_strv('enabled-extensions');
    const updated = current.filter(uuid => uuid !== UUID && uuid !== LEGACY_UUID);
    if (enabled) {
        updated.push(UUID);
        const disabled = settings.get_strv('disabled-extensions');
        if ((disabled.includes(UUID) || disabled.includes(LEGACY_UUID)) && !settings.set_strv(
            'disabled-extensions', disabled.filter(uuid => uuid !== UUID && uuid !== LEGACY_UUID)))
            throw new Error('GNOME extension settings are not writable');
    }
    if (JSON.stringify(updated) !== JSON.stringify(current) &&
        !settings.set_strv('enabled-extensions', updated))
        throw new Error('GNOME extension settings are not writable');
    Gio.Settings.sync();
    GLib.mkdir_with_parents(GLib.path_get_dirname(preferencePath()), 0o755);
    Gio.File.new_for_path(preferencePath()).replace_contents(
        JSON.stringify({auto_start: enabled}) + '\n', null, false,
        Gio.FileCreateFlags.REPLACE_DESTINATION, null);
}
