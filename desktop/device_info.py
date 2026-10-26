"""Readable adapter labels; saved interface names remain unchanged."""
from pathlib import Path


def selectable_interfaces(names):
    """Generated AP children are owned by RelayForge, never user selections."""
    return [name for name in names if not name.startswith('rfap')]


def describe_device(name, wifi=False, sys_net=Path('/sys/class/net'), udev=Path('/run/udev/data')):
    if not name or '/' in name or name in ('.', '..'):
        return ('Unknown interface', '', '')
    link = sys_net / name

    def read(path):
        try:
            return path.read_text().strip()
        except (OSError, UnicodeError):
            return ''

    properties = {}
    index = read(link / 'ifindex')
    if index.isdecimal():
        for line in read(udev / ('n' + index)).splitlines():
            if line.startswith('E:') and '=' in line:
                key, value = line[2:].split('=', 1)
                properties[key] = value
    if wifi or (link / 'wireless').exists() or (link / 'phy80211').exists():
        kind = 'Wi-Fi'
    elif (link / 'bridge').exists():
        kind = 'Network bridge'
    elif (link / 'tun_flags').exists():
        kind = 'VPN / tunnel'
    elif read(link / 'type') == '772':
        kind = 'Loopback'
    elif (link / 'device').exists() and read(link / 'type') == '1':
        kind = 'Ethernet'
    elif link.exists() and 'virtual' in link.resolve().parts:
        kind = 'Virtual interface'
    else:
        kind = 'Network interface'
    model = properties.get('ID_MODEL_FROM_DATABASE') or properties.get('ID_MODEL', '').replace('_', ' ')
    vendor = properties.get('ID_VENDOR_FROM_DATABASE') or properties.get('ID_VENDOR', '').replace('_', ' ')
    hardware = ' '.join(part for part in (vendor, model) if part)
    driver = properties.get('ID_NET_DRIVER', '')
    title = f'{kind} — {name}'
    tooltip = '\n'.join(part for part in (title, hardware, f'Driver: {driver}' if driver else '') if part)
    return title, hardware, tooltip


def device_factory(describe, disabled=lambda name: False):
    from gi.repository import Gtk, Pango

    def setup(_factory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0)
        detail = Gtk.Label(xalign=0, max_width_chars=42, ellipsize=Pango.EllipsizeMode.END)
        detail.add_css_class('dim-label')
        box.append(title)
        box.append(detail)
        item.set_child(box)

    def bind(_factory, item):
        title, hardware, tooltip = describe(item.get_item().get_string())
        box = item.get_child()
        box.get_first_child().set_text(title)
        detail = box.get_last_child()
        detail.set_text(hardware)
        detail.set_visible(bool(hardware))
        box.set_tooltip_text(tooltip)
        blocked = disabled(item.get_item().get_string())
        box.set_sensitive(not blocked)
        item.set_selectable(not blocked)
        item.set_activatable(not blocked)

    factory = Gtk.SignalListItemFactory()
    factory.connect('setup', setup)
    factory.connect('bind', bind)
    return factory
