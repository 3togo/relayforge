"""Pure configuration schema and validation shared by the service and probes."""
import ipaddress
import re

CONFIG_DEFAULTS = {
    "WIFI_IFACE": "wlan0", "INTERNET_IFACE": "wlan0", "SSID": "Hotspot",
    "PASSPHRASE": "12345678", "FREQ_BAND": "auto", "CHANNEL": "default",
    "GATEWAY": "192.168.12.1", "WPA_VERSION": "2", "ETC_HOSTS": "0",
    "DHCP_DNS": "gateway", "NO_DNS": "0", "NO_DNSMASQ": "0", "HIDDEN": "0",
    "MAC_FILTER": "0", "MAC_FILTER_ACCEPT": "/etc/hostapd/hostapd.accept",
    "ISOLATE_CLIENTS": "0", "SHARE_METHOD": "nat", "IEEE80211N": "0",
    "IEEE80211AC": "0", "IEEE80211AX": "0", "NO_VIRT": "0", "USE_PSK": "0",
    "BACKEND": "create_ap",
}
CONFIG_FLAGS = frozenset({"ETC_HOSTS", "NO_DNS", "NO_DNSMASQ", "HIDDEN", "MAC_FILTER",
                          "ISOLATE_CLIENTS", "IEEE80211N", "IEEE80211AC", "IEEE80211AX",
                          "NO_VIRT", "USE_PSK"})


def validate_config(conf, partial=False):
    """Accept only supported, single-line values before persisting or starting."""
    if not isinstance(conf, dict):
        raise ValueError("Configuration must be a JSON object.")
    allowed = set(CONFIG_DEFAULTS) | {"COUNTRY"}
    for key, value in conf.items():
        if key not in allowed:
            raise ValueError("Unsupported configuration key.")
        if not isinstance(value, str) or len(value) > 4096 or not value.isprintable():
            raise ValueError(f"{key} must contain printable text on a single line.")
    if partial:
        return conf
    result = {**CONFIG_DEFAULTS, **conf}
    if result["BACKEND"] not in {"create_ap", "networkmanager"}:
        raise ValueError("BACKEND must be create_ap or networkmanager.")
    for key in CONFIG_FLAGS:
        if result[key] not in {"0", "1"}:
            raise ValueError(f"{key} must be 0 or 1.")
    for key in ("WIFI_IFACE", "INTERNET_IFACE"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", result[key]):
            raise ValueError(f"{key} must be a valid interface name (1–15 characters).")
    if not 1 <= len(result["SSID"].encode("utf-8")) <= 32:
        raise ValueError("SSID must be 1–32 bytes in UTF-8.")
    password = result["PASSPHRASE"]
    if result["USE_PSK"] == "1":
        if not re.fullmatch(r"[0-9a-fA-F]{64}", password):
            raise ValueError("PASSPHRASE must be a 64-digit hexadecimal PSK.")
    elif not password.isascii() or not 8 <= len(password) <= 63:
        raise ValueError("PASSPHRASE must be 8–63 printable ASCII characters.")
    if result["FREQ_BAND"] not in {"auto", "2.4", "5"}:
        raise ValueError("FREQ_BAND must be auto, 2.4, or 5.")
    channel = result["CHANNEL"]
    if channel != "default" and not (re.fullmatch(r"[0-9]{1,3}", channel) and 1 <= int(channel) <= 196):
        raise ValueError("CHANNEL must be default or a number from 1 to 196.")
    if result["WPA_VERSION"] not in {"1", "2", "3"}:
        raise ValueError("WPA_VERSION must be 1, 2, or 3.")
    if result["SHARE_METHOD"] not in {"nat", "bridge", "none"}:
        raise ValueError("SHARE_METHOD must be nat, bridge, or none.")
    try:
        gateway = ipaddress.IPv4Address(result["GATEWAY"])
        if gateway.is_multicast or gateway.is_unspecified or gateway.is_loopback or (int(gateway) & 255) in (0, 255):
            raise ValueError()
    except ValueError:
        raise ValueError("GATEWAY must be a usable IPv4 host address in a /24 subnet.") from None
    if result["DHCP_DNS"] != "gateway":
        try:
            for address in result["DHCP_DNS"].split(","):
                ipaddress.IPv4Address(address)
        except ValueError:
            raise ValueError("DHCP_DNS must be gateway or comma-separated IPv4 addresses.") from None
    path = result["MAC_FILTER_ACCEPT"]
    if not re.fullmatch(r"/etc/hostapd/[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise ValueError("MAC_FILTER_ACCEPT must be a path inside /etc/hostapd.")
    if "COUNTRY" in result and not re.fullmatch(r"[A-Z]{2}", result["COUNTRY"]):
        raise ValueError("COUNTRY must be a two-letter uppercase country code.")
    return result



def detect_wifi_interface(sys_class_net="/sys/class/net"):
    """Choose an adapter only when a fresh install has one unambiguous Wi-Fi device."""
    from pathlib import Path
    try:
        interfaces = [entry.name for entry in Path(sys_class_net).iterdir()
                      if (entry / "wireless").exists() or (entry / "phy80211").exists()]
    except OSError:
        return None
    return interfaces[0] if len(interfaces) == 1 else None


def initialize_factory_password(path, factory_path, ssid=None, wifi_interface=None, *, detect_interface=True):
    """Initialize a factory config once; preserve every customized config."""
    import os
    from pathlib import Path
    import secrets
    import tempfile
    path, factory_path = Path(path), Path(factory_path)
    if path.is_symlink() or not path.is_file():
        return False
    original = path.read_bytes()
    if original != factory_path.read_bytes():
        return False
    password = secrets.token_hex(16).encode('ascii')
    updated = original.replace(b'PASSPHRASE=12345678\n', b'PASSPHRASE=' + password + b'\n', 1)
    if updated == original:
        return False
    if ssid is not None:
        ssid = ssid.encode('utf-8')[:32].decode('utf-8', errors='ignore')
        validate_config({'SSID': ssid})
        updated = updated.replace(b'SSID=Hotspot\n', b'SSID=' + ssid.encode('utf-8') + b'\n', 1)
    interface = wifi_interface if wifi_interface is not None else (detect_wifi_interface() if detect_interface else None)
    if interface is not None:
        validate_config({'WIFI_IFACE': interface, 'INTERNET_IFACE': interface})
        for key in (b'WIFI_IFACE', b'INTERNET_IFACE'):
            updated = updated.replace(key + b'=wlan0\n', key + b'=' + interface.encode('ascii') + b'\n', 1)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.wifi-hotspot-', delete=False) as stream:
            temporary = stream.name
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return True


def repair_legacy_wifi_interface(path, wifi_interface=None, sys_class_net="/sys/class/net"):
    """Replace an absent legacy wlan0 default when exactly one Wi-Fi device exists."""
    import os
    from pathlib import Path
    import tempfile
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    network = Path(sys_class_net)
    if (network / 'wlan0').exists():
        return False
    interface = wifi_interface if wifi_interface is not None else detect_wifi_interface(network)
    if interface is None:
        return False
    validate_config({'WIFI_IFACE': interface, 'INTERNET_IFACE': interface})
    original = path.read_bytes()
    updated = original
    replacement = interface.encode('ascii')
    # An older installer could leave the first assignment as a literal value.
    updated = re.sub(rb'(?m)^WIFI_IFACE=INTERNET_IFACE=wlan0$',
                     b'WIFI_IFACE=' + replacement, updated, count=1)
    for key in (b'WIFI_IFACE', b'INTERNET_IFACE'):
        updated = re.sub(rb'(?m)^' + key + rb'=wlan0$', key + b'=' + replacement,
                         updated, count=1)
    if updated == original:
        return False
    metadata = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.wifi-hotspot-', delete=False) as stream:
            temporary = stream.name
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Initialize a fresh factory configuration securely.')
    parser.add_argument('config')
    parser.add_argument('factory')
    parser.add_argument('--ssid', help='Use this SSID only when initializing a factory config')
    parser.add_argument('--repair-interface', action='store_true',
                        help='Repair an absent legacy wlan0 default during package upgrade')
    parser.add_argument('--offline', action='store_true',
                        help='Initialize credentials without inspecting host hardware')
    args = parser.parse_args()
    # Image assembly uses the target's files but must not detect host adapters.
    if args.repair_interface:
        repair_legacy_wifi_interface(args.config)
    else:
        initialize_factory_password(args.config, args.factory, args.ssid, detect_interface=not args.offline)
