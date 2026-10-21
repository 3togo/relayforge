#!/usr/bin/env python3
"""Developer probe for NetworkManager-owned AP+STA; read-only unless --run."""

import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# Reuse the production regulatory checks without starting its service.
probe_root = Path(__file__).resolve().parents[1]
daemon_path = probe_root / "daemon/wifi-hotspot-daemon.py"
if not daemon_path.is_file():
    daemon_path = probe_root / "wifi-hotspot-daemon.py"
# Resolve sibling production modules when run directly or via the installed symlink.
sys.path.insert(0, str(daemon_path.parent))
spec = importlib.util.spec_from_file_location("relay_daemon", daemon_path)
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)

# Load the same production client in repository and installed tool layouts.
client_spec = importlib.util.spec_from_file_location(
    "relay_nm_client", daemon_path.with_name("nm_client.py"))
client_module = importlib.util.module_from_spec(client_spec)
client_spec.loader.exec_module(client_module)
NetworkManager = client_module.NetworkManager
NM_NAME = client_module.NM_NAME
NM_PATH = client_module.NM_PATH
NATIVE_RELAY_CAPABILITY = client_module.NATIVE_RELAY_CAPABILITY
NATIVE_RELAY_PARENT_KEY = "org.freedesktop.NetworkManager.wifi-relay.parent"


class ProbeFailure(RuntimeError):
    """Carry lifecycle evidence even when activation or observation fails."""
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class StopRequested(Exception):
    pass


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def capabilities(station):
    reader = daemon.WifiHotspotDaemon.__new__(daemon.WifiHotspotDaemon)
    reader._read_config_dict = lambda: {"WIFI_IFACE": station}
    return reader.get_capabilities()


def interfaces(output):
    """Parse iw dev's per-PHY inventory, including unnamed P2P devices."""
    phy, iface = None, None
    result = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("phy#"):
            phy = int(line[4:])
        elif line.startswith("Interface "):
            iface = line.split()[1]
        elif line.startswith("Unnamed/non-netdev interface"):
            iface = None
        elif line.startswith("type "):
            result.append({"phy": phy, "interface": iface, "type": line[5:]})
    return result


def inspect(station, ap_iface):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", station):
        raise ValueError("Invalid station interface name.")
    inventory = interfaces(command(["iw", "dev"]))
    selected = next((i for i in inventory if i["interface"] == station), None)
    if selected is None:
        raise ValueError("Station interface is absent from iw dev.")
    caps = capabilities(station)
    blockers = []
    if selected["type"] != "managed":
        blockers.append("The selected station is not a managed Wi-Fi interface.")
    if not caps["current_sta_ssid"]:
        blockers.append("Connect the station to Wi-Fi before testing.")
    if not caps["ap_sta_concurrent"]:
        blockers.append("The PHY does not advertise concurrent managed + AP support.")
    if not caps["current_sta_ap_allowed"]:
        blockers.append("The upstream channel is not permitted for this non-DFS AP test.")
    siblings = [i for i in inventory if i["phy"] == selected["phy"]
                and i["interface"] != station]
    # Conservatively reserve all AP/client slots; never take over existing devices.
    if any(i["type"] not in ("P2P-device", "monitor", "AP/VLAN") for i in siblings):
        blockers.append("Another AP or client interface already occupies this PHY.")
    if Path("/sys/class/net", ap_iface).exists():
        blockers.append("The proposed probe interface already exists.")
    profile = command(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", station])
    if not profile or profile == "--":
        blockers.append("NetworkManager has no active upstream connection profile.")
    nm_ap = command(["nmcli", "-g", "WIFI-PROPERTIES.AP", "device", "show", station])
    if nm_ap != "yes":
        blockers.append("NetworkManager does not advertise AP support on this station.")
    api = api_compatibility()
    if not api["add_and_activate_connection2"]:
        blockers.append("The running NetworkManager does not expose the required activation API.")
    return {
        "networkmanager_version": command(["nmcli", "--version"]),
        "driver": command(["nmcli", "-g", "GENERAL.DRIVER", "device", "show", station]),
        "networkmanager_advertises_ap": nm_ap == "yes",
        "api_compatibility": api,
        "integration": {"virtual_interface_lifecycle": ("networkmanager" if api.get("native_wifi_relay") else "external-helper"),
                        "upstream_gnome_ui": "not-integrated",
                        "extension_toggle": "relay-service-backend-selector",
                        "live_activation": "not-tested"},
        "station": station,
        "phy": f"phy{selected['phy']}",
        "upstream_frequency_mhz": caps["current_sta_frequency"],
        "band": caps["current_sta_band"],
        "channel": next((c["channel"] for c in caps["ap_channels"].get(
            caps["current_sta_band"], [])
            if c["frequency"] == caps["current_sta_frequency"]), None),
        "hardware_advertises_ap_sta": caps["ap_sta_concurrent"],
        "sibling_interfaces": siblings,
        "probe_interface": ap_iface,
        "eligible_for_live_probe": not blockers,
        "blockers": blockers,
    }


def upstream_snapshot(station):
    caps = capabilities(station)
    return (command(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", station]),
            caps["current_sta_ssid"], caps["current_sta_bssid"],
            caps["current_sta_frequency"])


def ensure_upstream(station, baseline):
    if upstream_snapshot(station) != baseline:
        raise RuntimeError("Upstream connection or channel changed; stopping the probe.")


def settings(report, ssid, password):
    """Build an ephemeral WPA2 AP pinned to the current upstream channel."""
    v = GLib.Variant
    return {
        "connection": {
            "id": v("s", "Wi-Fi Relay NM probe"), "uuid": v("s", str(uuid.uuid4())),
            "type": v("s", "802-11-wireless"), "autoconnect": v("b", False),
            "interface-name": v("s", report["probe_interface"]),
        },
        "802-11-wireless": {
            "ssid": v("ay", ssid.encode()), "mode": v("s", "ap"),
            "band": v("s", "bg" if report["band"] == "2.4" else "a"),
            "channel": v("u", report["channel"]),
        },
        "802-11-wireless-security": {
            "key-mgmt": v("s", "wpa-psk"), "psk": v("s", password),
            "proto": v("as", ["rsn"]), "pairwise": v("as", ["ccmp"]),
            "group": v("as", ["ccmp"]),
        },
        "ipv4": {"method": v("s", "shared")},
        "ipv6": {"method": v("s", "disabled")},
    }


def service_settings(report, config):
    profile = settings(report, config["SSID"], config["PASSPHRASE"])
    profile["connection"]["id"] = GLib.Variant("s", "Wi-Fi Relay Hotspot")
    profile["802-11-wireless"]["assigned-mac-address"] = GLib.Variant("s", "preserve")
    profile["802-11-wireless"]["hidden"] = GLib.Variant("b", config["HIDDEN"] == "1")
    profile["802-11-wireless"]["ap-isolation"] = GLib.Variant("i", int(config["ISOLATE_CLIENTS"]))
    profile["ipv4"]["address-data"] = GLib.Variant("aa{sv}", [{
        "address": GLib.Variant("s", config["GATEWAY"]), "prefix": GLib.Variant("u", 24)}])
    return profile



def api_compatibility():
    nm = NetworkManager()
    try:
        return nm.compatibility()
    finally:
        nm.close()


def write_credentials(path, ssid, password):
    """Create a private file exclusively; never overwrite an existing destination."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"ssid": ssid, "password": password}, handle)
            handle.write("\n")
    except BaseException:
        Path(path).unlink()
        raise


def client_observation(ap):
    """L2/L3 evidence only; neither neighbor discovery nor NM state proves DHCP."""
    dump = command(["iw", "dev", ap, "station", "dump"])
    authorized = set()
    for block in re.split(r"(?m)^Station ", dump)[1:]:
        if re.search(r"(?m)^\s*authorized:\s*yes\s*$", block):
            authorized.add(block.split()[0].lower())
    addresses = json.loads(command(["ip", "-j", "-4", "address", "show", "dev", ap]))
    subnets, gateway_addresses = [], []
    for interface in addresses:
        for addr in interface.get("addr_info", []):
            if addr.get("family") == "inet":
                gateway_addresses.append(addr["local"])
                subnets.append(ipaddress.ip_network(f"{addr['local']}/{addr['prefixlen']}", strict=False))
    neighbors = json.loads(command(["ip", "-j", "-4", "neighbor", "show", "dev", ap]))
    matched = set()
    for neighbor in neighbors:
        state = neighbor.get("state", [])
        state = state.split() if isinstance(state, str) else state
        if (neighbor.get("lladdr", "").lower() not in authorized
                or not set(state) & {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}):
            continue
        try:
            address = ipaddress.ip_address(neighbor.get("dst", ""))
        except ValueError:
            continue
        if any(address in subnet for subnet in subnets):
            matched.add(neighbor["lladdr"].lower())
    return {"authorized_clients": len(authorized), "clients_with_ipv4_neighbor": len(matched),
            "gateway_ipv4_addresses": gateway_addresses}


def wait_absent(present, timeout=5):
    deadline = time.monotonic() + timeout
    while present():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def cleanup_verification(ap, profile_uuid, credentials_file):
    """Verify owned resources, allowing NM time to discard its volatile profile."""
    checks = {}
    if ap:
        checks["interface_removed"] = not Path("/sys/class/net", ap).exists()
    if profile_uuid:
        checks["profile_removed"] = wait_absent(lambda: profile_uuid in command(
            ["nmcli", "-g", "UUID", "connection", "show"]).splitlines())
    if credentials_file:
        checks["credentials_removed"] = not Path(credentials_file).exists()
    return checks


def restore_ap_mode(ap):
    """Diagnostic workaround for NM resetting an owned AP to station mode."""
    info = command(["iw", "dev", ap, "info"])
    if not re.search(r"type managed\s*$", info, re.M):
        return False
    command(["ip", "link", "set", "dev", ap, "down"])
    command(["iw", "dev", ap, "set", "type", "__ap"])
    return True


def run_probe(report, ssid, hold_seconds, nm_factory=NetworkManager,
              credentials_file=None, require_client=False, restore_ap=False,
              service_config=None, stop_requested=lambda: False,
              event_callback=lambda event: None, ownership_callback=lambda data: None,
              expected_upstream_uuid=None):
    if not report["eligible_for_live_probe"]:
        raise RuntimeError("Live probe blocked: " + " ".join(report["blockers"]))
    if os.geteuid() != 0:
        raise RuntimeError("Live testing needs root to create a temporary Wi-Fi interface.")
    station, ap = report["station"], report["probe_interface"]
    fresh = inspect(station, ap)
    if not fresh["eligible_for_live_probe"]:
        raise RuntimeError("Live probe blocked: " + " ".join(fresh["blockers"]))
    baseline = upstream_snapshot(station)
    if expected_upstream_uuid is not None and baseline[0] != expected_upstream_uuid:
        raise RuntimeError("Waiting for the original Wi-Fi connection.")
    if not baseline[1] or baseline[3] != report["upstream_frequency_mhz"]:
        raise RuntimeError("Upstream changed since inspection; run inspection again.")
    native = report.get("api_compatibility", {}).get("native_wifi_relay") is True
    nm, active, created, credentials_created = None, None, False, False
    profile_uuid, failure, requested_stop = None, None, False
    cleanup_errors = []
    started = time.monotonic()
    events = []
    result = {"activation_verified": False, "cleanup_errors": cleanup_errors,
              "lifecycle": events, "outcome": "running", "client_connectivity_tested": False,
              "dhcp_verified": False, "dns_verified": False, "internet_verified": False,
              "interface_lifecycle": "networkmanager" if native else "external-helper"}

    def stage(name):
        if name == "active" and service_config and not native:
            ownership["mac"] = Path("/sys/class/net", ap, "address").read_text().strip()
            ownership_callback(ownership)
        events.append({"stage": name, "elapsed_seconds": round(time.monotonic() - started, 3)})
        event_callback({"event": "stage", "stage": name, "interface": ap, "band": report["band"]})

    def check_stop():
        if stop_requested():
            raise StopRequested()

    def activation_state():
        # Dispatch the reason signal even if the active object has already gone.
        try:
            state = nm.state(active)
        except Exception:
            if nm.user_disconnected() is True:
                result["user_disconnected"] = True
                raise StopRequested()
            raise
        if nm.user_disconnected() is True:
            result["user_disconnected"] = True
            raise StopRequested()
        return state

    def cleanup_stage(name):
        # Notification failures must never bypass network/resource cleanup.
        try:
            stage(name)
            return True
        except Exception as exc:
            cleanup_errors.append(f"notify_{name}: {exc}")
            return False

    try:
        stage("preparing")
        check_stop()
        password = service_config["PASSPHRASE"] if service_config else secrets.token_urlsafe(18)
        if credentials_file:
            write_credentials(credentials_file, ssid, password)
            credentials_created = True
        if native:
            nm = nm_factory()
            if not nm.compatibility().get("native_wifi_relay"):
                raise RuntimeError("Native Wi-Fi Relay support disappeared before activation.")
            # NetworkManager realizes the child from the profile and owns its cleanup.
            # No helper-created interface or interface-deletion journal is needed.
            device = "/"
            stage("native-interface-request")
        else:
            mac = "02:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(5))
            command(["iw", "dev", station, "interface", "add", ap, "type", "__ap", "addr", mac])
            created = True
            ownership = {"interface": ap, "mac": mac}
            if service_config:
                ownership["ifindex"] = int(Path("/sys/class/net", ap, "ifindex").read_text())
                ownership_callback(ownership)
            stage("interface-created")
            nm = nm_factory()
            deadline = time.monotonic() + 10
            while True:
                check_stop()
                try:
                    device = nm.device(ap)
                    break
                except GLib.Error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("NetworkManager did not discover the virtual interface.")
                    time.sleep(0.25)
            command(["nmcli", "device", "set", ap, "autoconnect", "no", "managed", "yes"])
            stage("waiting-for-device")
            deadline = time.monotonic() + (30 if restore_ap else 15)
            mode_restored = False
            while True:
                check_stop()
                ensure_upstream(station, baseline)
                state, reason = nm.device_state(device)
                result["device_state"] = {"state": state, "reason": reason}
                if state == 30:  # NM_DEVICE_STATE_DISCONNECTED: ready for activation
                    break
                if restore_ap and state == 20 and not mode_restored:
                    mode_restored = restore_ap_mode(ap)
                    if mode_restored:
                        result["ap_mode_restored"] = True
                        stage("restoring-ap-mode")
                        stage("waiting-for-device")
                if state > 30 or time.monotonic() >= deadline:
                    raise RuntimeError("Virtual Wi-Fi device did not become ready for activation "
                                       f"(state {state}, reason {reason}); check NetworkManager's "
                                       "journal for supplicant/driver initialization failures.")
                time.sleep(0.25)
        ensure_upstream(station, baseline)
        check_stop()
        profile = service_settings(report, service_config) if service_config else settings(report, ssid, password)
        if native:
            profile["user"] = {"data": GLib.Variant("a{ss}", {NATIVE_RELAY_PARENT_KEY: station})}
            profile["802-11-wireless"]["assigned-mac-address"] = GLib.Variant("s", "preserve")
        profile_uuid = profile["connection"]["uuid"].unpack()
        if service_config and not native:
            ownership["profile_uuid"] = profile_uuid
            ownership["profile_id"] = profile["connection"]["id"].unpack()
            ownership_callback(ownership)
        stage("activating")
        active = nm.activate(device, profile)
        deadline = time.monotonic() + 30
        while True:
            check_stop()
            state = activation_state()
            ensure_upstream(station, baseline)
            if state == 2:  # NM_ACTIVE_CONNECTION_STATE_ACTIVATED
                break
            if state == 4 or time.monotonic() >= deadline:
                raise RuntimeError(f"Hotspot activation failed or timed out (state {state}).")
            time.sleep(0.25)
        result["activation_verified"] = True
        stage("active")
        print("Hotspot activated; client observation started.", file=sys.stderr)
        deadline = time.monotonic() + hold_seconds if hold_seconds is not None else None
        evidence = {"authorized_clients_peak": 0, "clients_with_ipv4_neighbor_peak": 0,
                    "gateway_ipv4_addresses": []}
        result["client_observation"] = evidence
        while True:
            check_stop()
            if activation_state() != 2:
                raise RuntimeError("NetworkManager hotspot stopped during observation.")
            ensure_upstream(station, baseline)
            info = command(["iw", "dev", ap, "info"])
            frequency = re.search(r"\((\d+(?:\.\d+)?) MHz\)", info)
            if (not re.search(r"type AP\s*$", info, re.M) or not frequency
                    or int(float(frequency[1])) != baseline[3]):
                raise RuntimeError("Virtual AP is not broadcasting on the upstream channel.")
            observed = client_observation(ap)
            evidence["authorized_clients_peak"] = max(evidence["authorized_clients_peak"],
                                                       observed["authorized_clients"])
            evidence["clients_with_ipv4_neighbor_peak"] = max(
                evidence["clients_with_ipv4_neighbor_peak"], observed["clients_with_ipv4_neighbor"])
            evidence["gateway_ipv4_addresses"] = observed["gateway_ipv4_addresses"]
            event_callback({"event": "clients", **observed})
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if require_client and not evidence["clients_with_ipv4_neighbor_peak"]:
            raise RuntimeError("No authorized client with an IPv4 neighbor was observed; "
                               "DHCP/DNS/internet remain unverified.")
        result.update(activation_verified=True, upstream_preserved=True,
                      observation_seconds=hold_seconds, client_observation=evidence,
                      client_connectivity_tested=False,
                      dhcp_verified=False, dns_verified=False, internet_verified=False)
    except StopRequested:
        requested_stop = True
    except Exception as exc:
        failure = str(exc) or type(exc).__name__
        result["failure_stage"] = events[-1]["stage"] if events else "preparing"
        result["error"] = failure
    finally:
        cleanup_stage("stopping")
        # Attempt every cleanup step even if activation or a previous step failed.
        actions = []
        if active:
            actions.append(("deactivate", lambda: nm.deactivate(active)))
        if nm:
            actions.append(("close_bus", nm.close))
        if created:
            actions.append(("delete_interface", lambda: command(["iw", "dev", ap, "del"])))
        if credentials_created:
            actions.append(("delete_credentials", lambda: Path(credentials_file).unlink()))
        for label, action in actions:
            try:
                action()
            except Exception as exc:
                cleanup_errors.append(f"{label}: {exc}")
        if created or credentials_created or (native and nm):
            try:
                if native and not wait_absent(lambda: Path("/sys/class/net", ap).exists()):
                    cleanup_errors.append("Native Wi-Fi Relay interface cleanup timed out.")
                result["cleanup_verification"] = cleanup_verification(
                    ap if created or native else None, profile_uuid,
                    credentials_file if credentials_created else None)
                for name, passed in result["cleanup_verification"].items():
                    if not passed:
                        cleanup_errors.append(f"verification failed: {name}")
            except Exception as exc:
                cleanup_errors.append(f"verify_cleanup: {exc}")
        try:
            ensure_upstream(station, baseline)
            result["upstream_preserved_after_cleanup"] = True
        except Exception as exc:
            result["upstream_preserved_after_cleanup"] = False
            if not failure:
                failure = str(exc) or type(exc).__name__
                result["failure_stage"] = "stopping"
                result["error"] = failure
        result["outcome"] = "failed" if failure or cleanup_errors else ("stopped" if requested_stop else "passed")
        if not cleanup_stage(result["outcome"]):
            result["outcome"] = "failed"
            if events[-1]["stage"] != "failed":
                cleanup_stage("failed")
        if cleanup_errors:
            print(json.dumps({"cleanup_errors": cleanup_errors}), file=sys.stderr)
    if failure:
        raise ProbeFailure(failure, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", required=True, help="Connected upstream Wi-Fi interface")
    parser.add_argument("--run", action="store_true", help="Run a temporary live test (root required)")
    parser.add_argument("--ssid", default="Wi-Fi Relay NM Probe")
    parser.add_argument("--hold-seconds", type=int, default=15)
    parser.add_argument("--credentials-file", type=Path,
                        help="Create a temporary mode-0600 JSON file for connecting a test client")
    parser.add_argument("--require-client", action="store_true",
                        help="Fail unless an authorized client with an IPv4 neighbor is observed")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Repeat complete start/observe/stop cycles (1–10; live mode only)")
    parser.add_argument("--restore-ap-mode", action="store_true",
                        help="Diagnostic: restore owned AP mode during unavailable-device retry")
    args = parser.parse_args()
    if not 1 <= len(args.ssid.encode()) <= 32 or not 1 <= args.hold_seconds <= 300:
        parser.error("SSID must be 1–32 UTF-8 bytes; hold time must be 1–300 seconds.")
    if (args.credentials_file or args.require_client or args.restore_ap_mode) and not args.run:
        parser.error("Live-test options require --run.")
    if args.require_client and not args.credentials_file:
        parser.error("--require-client needs --credentials-file so a client can join.")
    if not 1 <= args.cycles <= 10 or (args.cycles != 1 and not args.run):
        parser.error("--cycles must be 1–10; repeated cycles require --run.")

    def interrupted(signum, frame):
        raise InterruptedError(f"Probe interrupted by signal {signum}.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    report = None
    try:
        report = inspect(args.station, "wrnm" + secrets.token_hex(3))
        if args.run:
            report["cycles"] = []
            for cycle in range(args.cycles):
                if cycle:
                    report.update(inspect(args.station, "wrnm" + secrets.token_hex(3)))
                try:
                    result = run_probe(report, args.ssid, args.hold_seconds,
                                       credentials_file=args.credentials_file,
                                       require_client=args.require_client,
                                       restore_ap=args.restore_ap_mode)
                except ProbeFailure as exc:
                    report["cycles"].append(exc.result)
                    report["live_test"] = exc.result
                    report.setdefault("integration", {})["live_activation"] = (
                        "observed" if exc.result["activation_verified"] else "failed")
                    raise
                report["cycles"].append(result)
                report["live_test"] = result
                report.setdefault("integration", {})["live_activation"] = (
                    "observed" if result["activation_verified"] else "failed")
                if result["cleanup_errors"]:
                    break
        print(json.dumps(report, indent=2))
        return int(bool(report.get("live_test", {}).get("cleanup_errors")))
    except (RuntimeError, ValueError, OSError, GLib.Error, InterruptedError,
            subprocess.TimeoutExpired, ET.ParseError) as exc:
        error = {"error": str(exc)}
        if report is not None:
            error["report"] = report
        print(json.dumps(error, indent=2))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
