#!/usr/bin/env python3
"""
GNOME Wi-Fi Hotspot D-Bus Daemon
Exposes io.github.erhanzeyrek.WifiHotspot on the system D-Bus.
Manages create_ap, automatic 2.4/5GHz band checking, and firewalld DHCP/DNS fixes.
"""

import sys
import os
import json
import subprocess
import shutil
import re
import tempfile
import importlib.util
import signal
import select
import stat
import time
from pathlib import Path
import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

BUS_NAME = "io.github.erhanzeyrek.WifiHotspot"
OBJECT_PATH = "/io/github/erhanzeyrek/WifiHotspot"
INTERFACE_NAME = "io.github.erhanzeyrek.WifiHotspot"
MANAGE_ACTION = INTERFACE_NAME + ".manage"
PROTECTED_METHODS = frozenset({"Start", "Stop", "SetConfig", "GetConfig",
                               "SwitchBandAndReconnect", "PrepareFirewall"})
try:
    from configuration import CONFIG_DEFAULTS, CONFIG_FLAGS, validate_config
except ModuleNotFoundError:
    from daemon.configuration import CONFIG_DEFAULTS, CONFIG_FLAGS, validate_config
try:
    import legacy_runtime
    from operations import Operations
except ModuleNotFoundError:
    from daemon import legacy_runtime
    from daemon.operations import Operations

# Config file paths
SYSTEM_CONFIG = "/etc/wifi-hotspot.conf"
LOCAL_CONFIG = os.path.expanduser("~/.config/wifi-hotspot.conf")
DEV_CONFIG = os.path.join(os.path.dirname(__file__), "..", "data", "wifi-hotspot.conf")

RUNTIME_DIRECTORY = Path("/run/wifi-relay")
MAX_STARTUP_LOG_BYTES = 16384
CREATE_AP_PIDFILE = RUNTIME_DIRECTORY / "create-ap.pid"
PROC_DIRECTORY = Path("/proc")


def stale_relay_hostapd_pids(proc_root=PROC_DIRECTORY, runtime=RUNTIME_DIRECTORY):
    """Find Relay hostapd processes whose create_ap instance already cleaned up."""
    found = []
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return found
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            if os.readlink(process / "exe") != "/usr/sbin/hostapd":
                continue
            args = (process / "cmdline").read_bytes()[:8192].rstrip(b"\0").split(b"\0")
            if not args or args[0] != b"/usr/sbin/hostapd":
                continue
            config = Path(os.fsdecode(args[-1]))
            if (config.name != "hostapd.conf"
                    or config.parent.parent != runtime / "create-ap"
                    or not re.fullmatch(r"create_ap\.[A-Za-z0-9_.:-]{1,15}\.conf\.[A-Za-z0-9]{8}",
                                        config.parent.name)
                    or config.parent.exists()):
                continue
            found.append(int(process.name))
        except (OSError, ValueError):
            continue
    return found


def prepare_startup_log():
    """Open logs only inside a private, service-owned runtime directory."""
    RUNTIME_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(RUNTIME_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError("Hotspot runtime directory must be private and service-owned.")
        descriptor = os.open("create_ap.log", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise RuntimeError("Unsafe hotspot startup log.")
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    return str(RUNTIME_DIRECTORY / "create_ap.log")


def startup_log_error(path):
    # Bound both memory use and the D-Bus error reply, including a single long line.
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - MAX_STARTUP_LOG_BYTES))
        lines = [line.strip() for line in stream.read(MAX_STARTUP_LOG_BYTES).decode("utf-8", errors="replace").splitlines()
                 if line.strip()]
    return " \n".join(lines[-4:])


INTROSPECTION_XML = f"""
<node>
  <interface name="{INTERFACE_NAME}">
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
    <method name="GetInterfaces">
      <arg type="s" name="ifaces_json" direction="out"/>
    </method>
    <method name="GetCapabilities">
      <arg type="s" name="caps_json" direction="out"/>
    </method>
    <method name="SwitchBandAndReconnect">
      <arg type="s" name="target_band" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="PrepareFirewall">
      <arg type="b" name="success" direction="out"/>
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
</node>
"""


def find_create_ap():
    """Locate create_ap executable."""
    local_path = os.path.join(os.path.dirname(__file__), "create_ap")
    if os.path.exists(local_path) and os.access(local_path, os.X_OK):
        return local_path
    usr_libexec = "/usr/libexec/wifi-hotspot-daemon/create_ap"
    if os.path.exists(usr_libexec) and os.access(usr_libexec, os.X_OK):
        return usr_libexec
    which_path = shutil.which("create_ap")
    if which_path:
        return which_path
    return "create_ap"


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


def get_config_path(for_write=False):
    """Determine appropriate config path."""
    if os.path.exists(SYSTEM_CONFIG) or (for_write and os.access("/etc", os.W_OK)):
        return SYSTEM_CONFIG
    if os.path.exists(LOCAL_CONFIG) or for_write:
        os.makedirs(os.path.dirname(LOCAL_CONFIG), exist_ok=True)
        return LOCAL_CONFIG
    if os.path.exists(DEV_CONFIG):
        return DEV_CONFIG
    return SYSTEM_CONFIG


class WifiHotspotDaemon:
    def __init__(self, connection):
        self.connection = connection
        self.create_ap_bin = find_create_ap()
        self._legacy_process = None
        self.cached_clients = []
        self.cached_status_active = False
        self.is_starting = False
        self._operations = Operations()
        self._status_snapshot = {"active": False, "client_count": 0}
        self._stopping = False
        backend_spec = importlib.util.spec_from_file_location(
            "relay_nm_backend", Path(__file__).resolve().with_name("nm_backend.py"))
        backend_module = importlib.util.module_from_spec(backend_spec)
        backend_spec.loader.exec_module(backend_module)
        self.nm_backend = backend_module.Backend(recover_on_init=False)
        self.nm_backend.cancelled = self._operations.cancelled.is_set
        print(f"[*] WifiHotspotDaemon initialized. Using create_ap: {self.create_ap_bin}")

        # Subscribe to NetworkManager WirelessEnabled property changes
        try:
            self.connection.signal_subscribe(
                "org.freedesktop.NetworkManager",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                Gio.DBusSignalFlags.NONE,
                self._on_nm_properties_changed,
                None,
            )
            print("[*] Subscribed to NetworkManager property changes.")
        except Exception as e:
            print(f"[!] Warning: Could not subscribe to NM signals: {e}")

        # Periodic check for status, Wi-Fi radio & connected clients (every 2s)
        GLib.timeout_add_seconds(2, self._queue_poll)
        self._suspending = False
        self.connection.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager",
            "PrepareForSleep", "/org/freedesktop/login1", None,
            Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep, None)

    def _on_prepare_for_sleep(self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        sleeping = bool(parameters.unpack()[0])
        self._suspending = sleeping
        if getattr(self, '_operations', None) is not None:
            self._operations.submit(lambda: self.nm_backend.prepare_for_sleep(sleeping),
                                   self._background_finished, urgent=True, cancel=sleeping)
        else:
            self.nm_backend.prepare_for_sleep(sleeping)

    def _on_nm_properties_changed(self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        operations = getattr(self, '_operations', None)
        if self.is_starting and operations is None:
            return
        try:
            interface, changed_props, _ = parameters.unpack()
            if "WirelessEnabled" in changed_props:
                wireless_enabled = bool(changed_props["WirelessEnabled"])
                print(f"[*] NetworkManager WirelessEnabled changed: {wireless_enabled}")
                if not wireless_enabled and not getattr(self, "_suspending", False):
                    if operations is not None:
                        operations.submit(self.method_stop, self._background_finished,
                                          urgent=True, cancel=True)
                        return
                    status = self._get_status_dict()
                    if status["active"] or status.get("desired_active"):
                        print("[*] Wi-Fi turned off. Automatically stopping hotspot...", flush=True)
                        self.method_stop()
        except Exception as e:
            print(f"[!] Error handling NM property change: {e}")

    def _emit_signal(self, signal_name, json_str):
        if signal_name == "StatusChanged":
            self._status_snapshot = json.loads(json_str)
        try:
            self.connection.emit_signal(
                None,
                OBJECT_PATH,
                INTERFACE_NAME,
                signal_name,
                GLib.Variant("(s)", (json_str,)),
            )
        except Exception as e:
            print(f"[!] Error emitting signal {signal_name}: {e}")

    def _background_finished(self, value, error):
        if error:
            print(f"[!] Background hotspot operation failed: {error}", flush=True)

    def _queue_poll(self):
        self._operations.submit(self._periodic_poll, self._background_finished)
        return GLib.SOURCE_CONTINUE

    def _periodic_poll(self):
        try:
            config = self._read_config_dict()
            if self._owned_create_ap_pid() is None:
                self._recover_legacy()
            if config.get("BACKEND") == "networkmanager":
                self.nm_backend.poll(config)
            status = self._get_status_dict()
            self._status_snapshot = dict(status)
            if status.get("error") and status["error"] != getattr(self, "_last_nm_error", None):
                self._last_nm_error = status["error"]
                self._emit_signal("UserActionRequired", json.dumps({
                    "type": "error", "message": status["error"]}))

            # Turning the radio off cancels pending recovery as well.
            if status["active"] or status.get("desired_active"):
                code, out, _ = self._run_cmd(["nmcli", "radio", "wifi"])
                if (code == 0 and out.strip().lower() == "disabled"
                        and not getattr(self, "_suspending", False)):
                    print("[*] Wi-Fi disabled detected via poll. Stopping hotspot...", flush=True)
                    self.method_stop()
                    return GLib.SOURCE_CONTINUE

            # 2. Check if active status changed (e.g. process died or interface removed)
            if (status["active"] != self.cached_status_active or
                    status.get("state") != getattr(self, "_cached_state", None) or
                    status.get("desired_active") != getattr(self, "_cached_desired", None)):
                self._cached_state = status.get("state")
                self._cached_desired = status.get("desired_active")
                self.cached_status_active = status["active"]
                self._emit_signal("StatusChanged", json.dumps(status))

            # 3. Check connected clients
            if status["active"]:
                clients = self._get_clients_list(status.get("iface", "wlp2s0"))
                if clients != self.cached_clients:
                    self.cached_clients = clients
                    self._emit_signal("ClientsChanged", json.dumps(clients))
            else:
                if self.cached_clients:
                    self.cached_clients = []
                    self._emit_signal("ClientsChanged", json.dumps([]))
        except Exception as e:
            # Keep polling after transient failures, but make them diagnosable.
            error = str(e)
            if error != getattr(self, "_last_poll_error", None):
                print(f"[!] Hotspot status poll failed: {error}", flush=True)
                self._last_poll_error = error
        else:
            self._last_poll_error = None
        return GLib.SOURCE_CONTINUE

    # ------------------ Wi-Fi & System Helpers ------------------ #

    def _run_cmd(self, args, timeout=10):
        try:
            self._check_cancelled()
            with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env={**os.environ, "LC_ALL": "C"}) as process:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        self._check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(f'{args[0]} timed out')
                        out, err = process.communicate(timeout=min(.2, remaining))
                        return process.returncode, out.strip(), err.strip()
                    except subprocess.TimeoutExpired:
                        continue
                    except Exception:
                        process.terminate()
                        try:
                            process.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate()
                        raise
        except Exception as e:
            return -1, "", str(e)

    def _check_cancelled(self):
        operations = getattr(self, '_operations', None)
        if operations is not None and operations.cancelled.is_set() and not self._stopping:
            raise RuntimeError('Hotspot operation cancelled.')

    def _recover_legacy(self):
        try:
            legacy_runtime.recover_owned(CREATE_AP_PIDFILE, self.create_ap_bin)
        except Exception as exc:
            self._legacy_error = str(exc)
            self._emit_signal('StatusChanged', json.dumps({
                **getattr(self, '_status_snapshot', {'active': False}), 'error': str(exc)}))
            raise
        self._legacy_error = None

    def request_shutdown(self, finished):
        def stopped(value, error):
            self._background_finished(value, error)
            self._shutdown_complete = True
            self._operations.close()
            finished()
        self._operations.submit(self.method_stop, stopped, urgent=True, cancel=True)

    def _owned_create_ap_pid(self):
        """Never adopt or stop a separately launched hotspot or a reused PID."""
        try:
            child = getattr(self, '_legacy_process', None)
            if child is not None and child.poll() is None:
                # Cancellation can arrive before the shell publishes its PID.
                pid = child.pid
            else:
                with CREATE_AP_PIDFILE.open("rb") as stream:
                    value = stream.read(32).strip()
                if not re.fullmatch(rb"[1-9][0-9]{0,9}", value):
                    return None
                pid = int(value)
            process = PROC_DIRECTORY / str(pid)
            if process.stat().st_uid != os.geteuid():
                return None
            with (process / "cmdline").open("rb") as stream:
                args = stream.read(8192).decode("utf-8").split("\0")
            marker = args.index("--pidfile")
            if (args[marker + 1] != str(CREATE_AP_PIDFILE)
                    or self.create_ap_bin not in args):
                return None
            return pid
        except (OSError, ValueError, IndexError, UnicodeError):
            return None

    def _launch_create_ap(self, args, log_path):
        """Own the shell directly, including the interval before PID publication."""
        self._check_cancelled()
        with open(log_path, 'ab') as output:
            self._legacy_process = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, 'LC_ALL': 'C', 'RUNNING_AS_DAEMON': '1'})
        return 0, '', ''

    def _signal_create_ap_stop(self, pid):
        # --stop searches the running-instance list. A starting shell is not
        # listed yet, so that command can silently miss an authorized Stop.
        try:
            identity = legacy_runtime.process_identity(pid)
            descriptor = os.pidfd_open(pid)
        except (FileNotFoundError, ProcessLookupError):
            return False
        try:
            if self._owned_create_ap_pid() == pid and legacy_runtime.same_process(identity):
                signal.pidfd_send_signal(descriptor, signal.SIGUSR1)
                return True
        except ProcessLookupError:
            pass
        finally:
            os.close(descriptor)
        return False

    def _reap_stale_hostapd(self):
        """Release only orphaned hostapd processes from Relay's private runtime."""
        for pid in stale_relay_hostapd_pids():
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                if pid not in stale_relay_hostapd_pids():
                    continue
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                watcher = select.poll()
                watcher.register(descriptor, select.POLLIN)
                if not watcher.poll(3000):
                    raise RuntimeError("A stale Relay hostapd process did not exit; hotspot startup was cancelled.")
            finally:
                os.close(descriptor)

    def _get_status_dict(self):
        nm = getattr(self, "nm_backend", None)
        if nm is not None:
            status = nm.get_status()
            if nm.process or self._read_config_dict().get("BACKEND", "create_ap") == "networkmanager":
                return status
        pid = self._owned_create_ap_pid()
        out = ""
        if pid is not None and self._create_ap_is_ready(pid):
            code, out, _ = self._run_cmd([self.create_ap_bin, "--list-running"])
            if code:
                out = ""
        owned = None
        for line in out.splitlines():
            row = re.fullmatch(r"([1-9][0-9]*)\s+([a-zA-Z0-9_.:-]{1,15})(?:\s+\(([a-zA-Z0-9_.:-]{1,15})\))?", line.strip())
            if row and int(row[1]) == pid:
                owned = row
                break
        if owned is not None:
            phy_iface = owned[2]
            virt_iface = owned[3] or phy_iface

            config = self._read_config_dict()
            # Report the actual AP band, including runtime fallback from a 5 GHz preference.
            actual_band = ""
            code_i, out_i, _ = self._run_cmd(["iw", "dev", virt_iface, "info"])
            if code_i == 0:
                match = re.search(r"\((\d+(?:\.\d+)?)\s*MHz\)", out_i)
                if match:
                    actual_band = self._frequency_band(float(match[1]))

            return {
                "active": bool(actual_band),
                "pid": pid,
                "iface": virt_iface,
                "phy_iface": phy_iface,
                "ssid": config.get("SSID", get_default_ssid()),
                "band": actual_band,
                "client_count": len(self.cached_clients),
            }
        return {
            "active": False,
            "pid": 0,
            "iface": "",
            "phy_iface": "",
            "ssid": "",
            "band": "",
            "client_count": 0,
            **({'error': self._legacy_error} if getattr(self, '_legacy_error', None) else {}),
        }

    def _get_clients_list(self, iface):
        nm = getattr(self, "nm_backend", None)
        if nm is not None and self._read_config_dict().get("BACKEND") == "networkmanager":
            if not nm.get_status().get("active"):
                return []
            code, output, _ = self._run_cmd(["iw", "dev", iface, "station", "dump"])
            if code:
                return []
            macs = [block.split()[0].lower() for block in re.split(r"(?m)^Station ", output)[1:]
                    if re.search(r"(?m)^\s*authorized:\s*yes\s*$", block)]
            code, output, _ = self._run_cmd(["ip", "-j", "-4", "neighbor", "show", "dev", iface])
            neighbors = json.loads(output) if code == 0 else []
            addresses = {n.get("lladdr", "").lower(): n.get("dst", "") for n in neighbors}
            return [{"mac": mac, "ip": addresses.get(mac, ""), "hostname": ""} for mac in macs]
        # 1. Get stations from iw
        code, out, _ = self._run_cmd(["iw", "dev", iface, "station", "dump"])
        macs = []
        if code == 0:
            for line in out.splitlines():
                if line.startswith("Station "):
                    parts = line.split()
                    if len(parts) >= 2:
                        macs.append(parts[1].lower())

        # Fallback to create_ap if iw fails
        if not macs:
            code, out, _ = self._run_cmd([self.create_ap_bin, "--list-clients", iface])
            if code == 0 and out.strip() and "No clients connected" not in out:
                for line in out.strip().split("\n"):
                    if line.startswith("MAC") or not line.strip():
                        continue
                    p = line.split()
                    if len(p) >= 1:
                        macs.append(p[0].lower())

        if not macs:
            return []

        # 2. Read dnsmasq leases for IP and Hostname
        leases = {}
        try:
            import glob
            for lease_file in glob.glob(str(RUNTIME_DIRECTORY / "create-ap/create_ap.*/dnsmasq.leases")):
                with open(lease_file, "r") as f:
                    for line in f:
                        parts = line.strip().split()
                        # format: <timestamp> <mac> <ip> <hostname> <client_id>
                        if len(parts) >= 4:
                            l_mac = parts[1].lower()
                            l_ip = parts[2]
                            l_host = parts[3] if parts[3] != "*" else ""
                            leases[l_mac] = {"ip": l_ip, "hostname": l_host}
        except Exception:
            pass

        # 3. Read ARP table (/proc/net/arp) as fallback for IP
        arp_table = {}
        try:
            with open("/proc/net/arp", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 6:
                        a_ip = parts[0]
                        a_mac = parts[3].lower()
                        arp_table[a_mac] = a_ip
        except Exception:
            pass

        # 4. Construct enriched client list
        clients = []
        for mac in macs:
            ip = leases.get(mac, {}).get("ip") or arp_table.get(mac, "")
            hostname = leases.get(mac, {}).get("hostname", "")

            # Names come from DHCP leases. Reverse DNS must not block D-Bus polling.

            clients.append({
                "mac": mac,
                "ip": ip,
                "hostname": hostname
            })

        return clients

    def _read_config_dict(self):
        path = get_config_path(for_write=False)
        conf = {}
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if line and not line.lstrip().startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        conf[k.strip()] = v
        return conf

    def _write_config_dict(self, conf):
        # Treat updates as patches, retaining settings absent from the UI.
        validate_config(conf, partial=True)
        merged = {**self._read_config_dict(), **conf}
        config = validate_config(merged)
        previous_backend = self._read_config_dict().get("BACKEND", "create_ap")
        status = self._get_status_dict() if config["BACKEND"] != previous_backend else {}
        if status.get("active") or status.get("desired_active"):
            raise ValueError("Stop the hotspot before changing its backend.")
        path = get_config_path(for_write=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                             prefix=".wifi-hotspot-", delete=False) as handle:
                temp_path = handle.name
                os.fchmod(handle.fileno(), 0o600)
                handle.write("# Wi-Fi Hotspot Configuration\n")
                for key, value in config.items():
                    handle.write(f"{key}={value}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                os.unlink(temp_path)
        return True

    def prepare_firewall(self):
        """Compatibility entry point; the selected backend owns runtime rules."""
        # Never open DHCP/DNS globally or reload an administrator's firewall.
        # create_ap installs/removes its runtime rules; NM owns shared-IP policy.
        return True

    @staticmethod
    def _frequency_band(freq):
        if 2400 <= freq < 2500:
            return "2.4"
        if 4900 <= freq < 5925:
            return "5"
        return ""

    @staticmethod
    def _split_nmcli_row(line):
        """Decode nmcli terse fields, including escaped colons in SSIDs/BSSIDs."""
        fields, field, escaped = [], [], False
        for char in line:
            if escaped:
                field.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == ":":
                fields.append("".join(field))
                field = []
            else:
                field.append(char)
        fields.append("".join(field))
        return fields

    @staticmethod
    def _parse_phy_capabilities(output):
        """Only allow channels usable without DFS/CAC by this backend."""
        mode_section = re.search(
            r"Supported interface modes:\n((?:[ \t]+\*[^\n]*\n)+)", output
        )
        has_ap = bool(mode_section and re.search(r"\* AP\s*$", mode_section[1], re.M))
        channels = {"2.4": [], "5": []}
        if has_ap:
            for match in re.finditer(r"\* (\d+(?:\.\d+)?) MHz \[(\d+)\]([^\n]*)", output):
                freq, channel, flags = float(match[1]), int(match[2]), match[3].lower()
                band = WifiHotspotDaemon._frequency_band(freq)
                if band and not any(flag in flags for flag in (
                    "disabled", "no ir", "no-ir", "passive scanning", "radar detection"
                )):
                    channels[band].append({"frequency": int(freq), "channel": channel})

        concurrent = False
        section = re.search(r"valid interface combinations:\n((?:[ \t]+[^\n]*\n)+)", output)
        if section:
            for combination in re.split(r"\n?\s*\* ", section[1]):
                limits = [(set(t.strip() for t in types.split(",")), int(limit))
                          for types, limit in re.findall(r"#\{([^}]+)\} <= (\d+)", combination)]
                total = re.search(r"total <= (\d+)", combination)
                # A shared {managed, AP} group needs room for both interfaces.
                if (total and int(total[1]) >= 2
                        and any("managed" in types for types, _ in limits)
                        and any("AP" in types for types, _ in limits)
                        and all(len(types & {"managed", "AP"}) <= limit for types, limit in limits)):
                    concurrent = has_ap
        return channels, concurrent

    def get_capabilities(self):
        """Read regulatory/channel restrictions from the configured adapter's PHY."""
        wifi_iface = self._read_config_dict().get("WIFI_IFACE", "wlan0")
        channels, concurrent = {"2.4": [], "5": []}, False
        code, info, _ = self._run_cmd(["iw", "dev", wifi_iface, "info"])
        phy = re.search(r"\bwiphy (\d+)", info) if code == 0 else None
        if phy:
            code, output, _ = self._run_cmd(["iw", "phy", "phy" + phy[1], "info"])
            if code == 0:
                channels, concurrent = self._parse_phy_capabilities(output)

        ssid, frequency, bssid = "", 0, ""
        code, link, _ = self._run_cmd(["iw", "dev", wifi_iface, "link"])
        if code == 0 and "Connected to" in link:
            for line in link.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    ssid = line.split(":", 1)[1].strip()
                elif line.startswith("freq:"):
                    match = re.search(r"\d+(?:\.\d+)?", line)
                    if match:
                        frequency = int(float(match[0]))
                elif line.startswith("Connected to "):
                    bssid = line.split()[2]
        band = self._frequency_band(frequency)
        return {
            "ap_2ghz": bool(channels["2.4"]),
            "ap_5ghz": bool(channels["5"]),
            "ap_channels": channels,
            "current_sta_band": band,
            "current_sta_frequency": frequency,
            "current_sta_ssid": ssid,
            "current_sta_bssid": bssid,
            "current_sta_ap_allowed": any(
                c["frequency"] == frequency for c in channels.get(band, [])
            ),
            "ap_sta_concurrent": concurrent,
            "wifi_iface": wifi_iface,
        }

    def switch_band_and_reconnect(self, target_band):
        """Reconnect the existing profile to a permitted BSSID and verify the result."""
        if target_band not in ("2.4", "5"):
            return json.dumps({"success": False, "error": "Unsupported Wi-Fi band."})
        caps = self.get_capabilities()
        ssid, iface = caps["current_sta_ssid"], caps["wifi_iface"]
        if not ssid:
            return json.dumps({"success": False, "error": "No active Wi-Fi connection to switch."})
        code, profile, err = self._run_cmd([
            "nmcli", "-g", "GENERAL.CON-UUID", "device", "show", iface
        ])
        if code or not profile or profile == "--":
            return json.dumps({"success": False, "error": err or "Cannot identify the active Wi-Fi profile."})
        code, scan, err = self._run_cmd([
            "nmcli", "--wait", "8", "-t", "-f", "SSID,BSSID,FREQ,SIGNAL",
            "device", "wifi", "list", "ifname", iface, "--rescan", "yes"
        ], timeout=10)
        if code:
            return json.dumps({"success": False, "error": err or "Wi-Fi scan failed."})
        allowed = {c["frequency"] for c in caps["ap_channels"][target_band]}
        candidates = []
        for line in scan.splitlines():
            fields = self._split_nmcli_row(line)
            if len(fields) != 4 or fields[0] != ssid:
                continue
            freq_match = re.fullmatch(r"(\d+)(?:\s*MHz)?", fields[2])
            if freq_match and int(freq_match[1]) in allowed:
                candidates.append((int(fields[3]) if fields[3].isdigit() else 0, fields[1]))
        if not candidates:
            return json.dumps({"success": False, "error":
                f"No reachable {target_band} GHz access point for '{ssid}' on a permitted hotspot channel. "
                "Enable that band on your router or use a second Wi-Fi adapter."})
        target_bssid = max(candidates)[1]
        code, _, err = self._run_cmd([
            "nmcli", "--wait", "8", "connection", "up", "uuid", profile,
            "ifname", iface, "ap", target_bssid
        ], timeout=10)
        updated = self.get_capabilities()
        if (code or updated["current_sta_band"] != target_band
                or updated["current_sta_ssid"] != ssid
                or not updated["current_sta_ap_allowed"]):
            # Some NetworkManager/driver combinations accept `ap BSSID` but
            # immediately select a stronger AP on the old band. Constrain the
            # saved profile for one activation, then restore its saved setting.
            band_setting = "bg" if target_band == "2.4" else "a"
            read_code, original_band, _ = self._run_cmd([
                "nmcli", "-g", "802-11-wireless.band", "connection", "show", "uuid", profile
            ])
            if read_code == 0:
                original_band = original_band.strip()
                changed_band = False
                restore_failure = None
                try:
                    if original_band != band_setting:
                        change_code, _, change_error = self._run_cmd([
                            "nmcli", "connection", "modify", "uuid", profile,
                            "802-11-wireless.band", band_setting
                        ])
                        changed_band = change_code == 0
                        if change_code:
                            err = change_error or "Could not restrict the upstream Wi-Fi band."
                    else:
                        changed_band = True
                    if changed_band:
                        code, _, err = self._run_cmd([
                            "nmcli", "--wait", "8", "connection", "up", "uuid", profile,
                            "ifname", iface, "ap", target_bssid
                        ], timeout=10)
                        updated = self.get_capabilities()
                finally:
                    if original_band != band_setting and changed_band:
                        restore_code, _, restore_error = self._run_cmd([
                            "nmcli", "connection", "modify", "uuid", profile,
                            "802-11-wireless.band", original_band
                        ])
                        if restore_code:
                            restore_failure = restore_error or "Could not restore the upstream Wi-Fi profile band."
                if restore_failure:
                    return json.dumps({"success": False, "error": restore_failure})
        if (code or updated["current_sta_band"] != target_band
                or updated["current_sta_ssid"] != ssid
                or not updated["current_sta_ap_allowed"]):
            # Best effort restore of the original AP after a failed band switch.
            restore = ["nmcli", "--wait", "8", "connection", "up", "uuid", profile, "ifname", iface]
            if caps["current_sta_bssid"]:
                restore += ["ap", caps["current_sta_bssid"]]
            self._run_cmd(restore, timeout=10)
            return json.dumps({"success": False, "error": err or "Wi-Fi did not reconnect on a permitted hotspot channel."})
        return json.dumps({"success": True, "target_band": target_band, "ssid": ssid})

    # ------------------ D-Bus Methods ------------------ #

    def _authorize(self, sender, callback):
        # Use the bus-assigned unique name, never a UID/PID supplied by the client.
        if not isinstance(sender, str) or not re.fullmatch(r":[0-9]+\.[0-9]+", sender):
            callback(False)
            return
        parameters = GLib.Variant("((sa{sv})sa{ss}us)", (
            ("system-bus-name", {"name": GLib.Variant("s", sender)}),
            MANAGE_ACTION, {}, 1, "",  # Allow interaction through the session's auth agent.
        ))
        def finished(connection, result):
            try:
                authorized, _challenge, _details = connection.call_finish(result).unpack()[0]
            except Exception:
                authorized = False  # Fail closed when Polkit is unavailable.
            callback(bool(authorized))
        try:
            self.connection.call(
                "org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
                "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization", parameters,
                GLib.VariantType.new("((bba{ss}))"), Gio.DBusCallFlags.NONE,
                60000, None, finished,
            )
        except Exception:
            callback(False)

    def handle_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if method_name not in PROTECTED_METHODS:
            self._dispatch_method_call(method_name, parameters, invocation)
            return
        def authorized(allowed):
            if not allowed:
                invocation.return_error_literal(
                    Gio.DBusError.quark(), Gio.DBusError.ACCESS_DENIED,
                    "Authorization is required to manage the hotspot or read its configuration.",
                )
                return
            self._dispatch_method_call(method_name, parameters, invocation)
        self._authorize(sender, authorized)

    def _dispatch_method_call(self, method_name, parameters, invocation):
        operations = getattr(self, '_operations', None)
        if operations is None:
            return self._dispatch_sync(method_name, parameters, invocation)
        if method_name in {'GetStatus', 'GetClients'}:
            if method_name == 'GetStatus':
                value = dict(self._status_snapshot)
                if operations.busy and operations.cancelled.is_set():
                    value['state'] = 'stopping'
            else:
                value = list(self.cached_clients)
            invocation.return_value(GLib.Variant('(s)', (json.dumps(value),)))
            return

        class Reply:
            def return_value(self, value):
                self.result = ('value', value)

            def return_error_literal(self, *error):
                self.result = ('error', error)

        def work():
            reply = Reply()
            self._dispatch_sync(method_name, parameters, reply)
            return reply.result

        def completed(reply, error):
            if error:
                invocation.return_error_literal(Gio.DBusError.quark(), Gio.DBusError.FAILED, str(error))
            elif reply[0] == 'value':
                invocation.return_value(reply[1])
            else:
                invocation.return_error_literal(*reply[1])

        if not operations.submit(work, completed, urgent=method_name == 'Stop', cancel=method_name == 'Stop',
                                 defer=method_name in {'GetConfig', 'GetInterfaces', 'GetCapabilities', 'SetConfig'}):
            invocation.return_error_literal(Gio.DBusError.quark(), Gio.DBusError.FAILED,
                                            'Another hotspot operation is in progress; retry shortly.')

    def _dispatch_sync(self, method_name, parameters, invocation):
        transition = {"Start": "connecting", "Stop": "stopping",
                      "SwitchBandAndReconnect": "connecting"}.get(method_name)
        try:
            if transition:
                self._emit_signal("StatusChanged", json.dumps({
                    "active": self.cached_status_active, "state": transition,
                    "client_count": len(self.cached_clients),
                }))
            if method_name == "Start":
                res = self.method_start()
                invocation.return_value(GLib.Variant("(s)", (res,)))
            elif method_name == "Stop":
                res = self.method_stop()
                invocation.return_value(GLib.Variant("(b)", (res,)))
            elif method_name == "GetStatus":
                res = json.dumps(self._get_status_dict())
                invocation.return_value(GLib.Variant("(s)", (res,)))
            elif method_name == "GetClients":
                status = self._get_status_dict()
                clients = self._get_clients_list(status.get("iface", "wlan0"))
                invocation.return_value(GLib.Variant("(s)", (json.dumps(clients),)))
            elif method_name == "GetConfig":
                conf = self._read_config_dict()
                invocation.return_value(GLib.Variant("(s)", (json.dumps(conf),)))
            elif method_name == "SetConfig":
                json_str = parameters.unpack()[0]
                if len(json_str.encode("utf-8")) > 65536:
                    raise ValueError("Configuration exceeds the 64 KiB limit")
                conf = json.loads(json_str)
                res = self._write_config_dict(conf)
                invocation.return_value(GLib.Variant("(b)", (res,)))
            elif method_name == "GetInterfaces":
                res = self.method_get_interfaces()
                invocation.return_value(GLib.Variant("(s)", (res,)))
            elif method_name == "GetCapabilities":
                res = json.dumps(self.get_capabilities())
                invocation.return_value(GLib.Variant("(s)", (res,)))
            elif method_name == "SwitchBandAndReconnect":
                band = parameters.unpack()[0]
                res = self.switch_band_and_reconnect(band)
                invocation.return_value(GLib.Variant("(s)", (res,)))
            elif method_name == "PrepareFirewall":
                res = self.prepare_firewall()
                invocation.return_value(GLib.Variant("(b)", (res,)))
            else:
                invocation.return_error_literal(
                    Gio.DBusError.quark(),
                    Gio.DBusError.UNKNOWN_METHOD,
                    f"Unknown method {method_name}",
                )
        except (ValueError, TypeError) as e:
            invocation.return_error_literal(Gio.DBusError.quark(), Gio.DBusError.INVALID_ARGS, str(e))
        except Exception as e:
            print(f"[!] Error in {method_name}: {e}")
            invocation.return_error_literal(
                Gio.DBusError.quark(),
                Gio.DBusError.FAILED,
                str(e),
            )

        finally:
            if transition:
                try:
                    self._emit_signal("StatusChanged", json.dumps(self._get_status_dict()))
                except Exception as e:
                    print(f"[!] Could not refresh hotspot status: {e}")

    def _create_ap_is_ready(self, pid):
        try:
            with Path(str(CREATE_AP_PIDFILE) + ".ready").open("rb") as stream:
                return stream.read(32).strip() == str(pid).encode("ascii")
        except OSError:
            return False

    def method_start(self):
        self.is_starting = True
        try:
            self._check_cancelled()
            self._recover_legacy()
            status = self._get_status_dict()
            if status["active"]:
                return json.dumps({"success": True, "message": "Already running", "active": True})

            config = validate_config(self._read_config_dict())
            if config["BACKEND"] == "networkmanager":
                try:
                    status = self.nm_backend.start(config)
                    self.cached_status_active = True
                    self._last_nm_error = None
                    return json.dumps({"success": True, "status": status})
                except Exception as exc:
                    return json.dumps({"success": False, "error": str(exc)})
            try:
                self._reap_stale_hostapd()
            except OSError as exc:
                return json.dumps({"success": False, "error": f"Could not clear stale Relay AP: {exc}"})
            except RuntimeError as exc:
                return json.dumps({"success": False, "error": str(exc)})
            caps = self.get_capabilities()
            self._check_cancelled()
            req_band = config.get("FREQ_BAND", "auto")
            if req_band not in ("auto", "2.4", "5"):
                return json.dumps({"success": False, "error": "Unsupported hotspot band."})
            connected = bool(caps["current_sta_ssid"])
            if connected:
                if not caps["ap_sta_concurrent"] or config.get("NO_VIRT", "0") == "1":
                    return json.dumps({"success": False, "error":
                        "This adapter/configuration cannot run Wi-Fi and a hotspot together. Use a second adapter."})
                target_band = req_band if req_band != "auto" else caps["current_sta_band"]
                if req_band == "auto" and not caps["current_sta_ap_allowed"]:
                    target_band = "2.4" if caps["ap_2ghz"] else "5"
                if (not caps["current_sta_ap_allowed"]
                        or (req_band != "auto" and caps["current_sta_band"] != req_band)):
                    if not caps["ap_channels"].get(target_band):
                        return json.dumps({"success": False, "error":
                            f"No permitted {target_band} GHz hotspot channel is available."})
                    print(f"[*] Switching upstream Wi-Fi to a permitted {target_band} GHz hotspot channel...")
                    result = json.loads(self.switch_band_and_reconnect(target_band))
                    if not result["success"]:
                        return json.dumps(result)
                    caps = self.get_capabilities()
                    if not caps["current_sta_ap_allowed"] or caps["current_sta_band"] != target_band:
                        return json.dumps({"success": False, "error": "Wi-Fi changed before hotspot startup; try again."})
                resolved_band = caps["current_sta_band"]
                channel = next(c["channel"] for c in caps["ap_channels"][resolved_band]
                               if c["frequency"] == caps["current_sta_frequency"])
            else:
                if config.get("WIFI_IFACE", "wlan0") == config.get("INTERNET_IFACE", "wlan0"):
                    return json.dumps({"success": False, "error": "Connect to Wi-Fi before sharing this adapter's connection."})
                resolved_band = req_band
                if req_band == "auto":
                    resolved_band = "5" if caps["ap_5ghz"] else "2.4"
                channels = caps["ap_channels"][resolved_band]
                if not channels:
                    return json.dumps({"success": False, "error": f"No permitted {resolved_band} GHz hotspot channel is available."})
                channel = config.get("CHANNEL", "default")
                if channel == "default":
                    channel = channels[0]["channel"]
                elif str(channel) not in {str(c["channel"]) for c in channels}:
                    return json.dumps({"success": False, "error": "The configured hotspot channel is not permitted."})

            self.prepare_firewall()

            # 4. Start create_ap process with logfile
            config_path = get_config_path(for_write=False)
            self._check_cancelled()
            log_path = prepare_startup_log()
            cmd = [self.create_ap_bin, "--config", config_path, "--logfile", log_path, "--pidfile", str(CREATE_AP_PIDFILE), "--daemon", "--freq-band", resolved_band, "-c", str(channel)]
            print(f"[*] Starting hotspot with: {' '.join(cmd)}")
            code, out, err = self._launch_create_ap(cmd, log_path)

            # The shell exists before DHCP and hostapd start. Wait for its
            # readiness acknowledgement, rather than treating its PID as an AP.
            for _ in range(40) if code == 0 else ():
                self._check_cancelled()
                GLib.usleep(500000) # 0.5s
                status = self._get_status_dict()
                if status["active"]:
                    break
            
            self.cached_status_active = status["active"]
            self._emit_signal("StatusChanged", json.dumps(status))

            if status["active"]:
                if connected:
                    upstream = self.get_capabilities()
                    if (status.get("iface") == caps["wifi_iface"]
                            or upstream["current_sta_ssid"] != caps["current_sta_ssid"]
                            or upstream["current_sta_frequency"] != caps["current_sta_frequency"]):
                        self.method_stop()
                        return json.dumps({"success": False, "error":
                            "The hotspot could not keep the upstream Wi-Fi connection active. "
                            "Concurrent Wi-Fi + hotspot startup was cancelled."})
                return json.dumps({"success": True, "status": status})
            else:
                err_msg = ""
                if os.path.exists(log_path):
                    try:
                        err_msg = startup_log_error(log_path)
                    except Exception:
                        pass
                if not err_msg:
                    err_msg = err or out or "Failed to start AP"
                self.method_stop()
                print(f"[!] Hotspot start failed: {err_msg}")
                return json.dumps({"success": False, "error": err_msg})
        finally:
            self.is_starting = False

    def method_stop(self):
        self._stopping = True
        try:
            return self._stop_backends()
        finally:
            self._stopping = False

    def _stop_backends(self):
        nm = getattr(self, "nm_backend", None)
        if nm is not None and (nm.process or self._read_config_dict().get("BACKEND", "create_ap") == "networkmanager"):
            result = nm.stop()
            self.cached_status_active = False
            self.cached_clients = []
            self._emit_signal("StatusChanged", json.dumps(nm.get_status()))
            self._emit_signal("ClientsChanged", json.dumps([]))
            return result
        pid = self._owned_create_ap_pid()
        if pid is None:
            self._recover_legacy()
            self._reap_stale_hostapd()
            return True

        print(f"[*] Stopping hotspot for: {pid}")
        signalled = self._signal_create_ap_stop(pid)

        # Cleanup may wait for child termination and NetworkManager. Do not
        # contend on create_ap's global mutex by polling --list-running here.
        # This wait runs on the operation worker, leaving D-Bus responsive.
        for _ in range(300):
            GLib.usleep(100000)
            remaining = self._owned_create_ap_pid()
            if remaining is None:
                break
            if not signalled:
                # The new child may have exec'd between identity checks.
                signalled = self._signal_create_ap_stop(remaining)
        new_status = self._get_status_dict()
        self.cached_clients = []
        self._emit_signal("StatusChanged", json.dumps(new_status))
        self._emit_signal("ClientsChanged", json.dumps([]))
        if self._owned_create_ap_pid() is not None:
            return False
        self._recover_legacy()
        return True

    def method_get_interfaces(self):
        wifi_ifaces = []
        all_ifaces = []

        code, out, _ = self._run_cmd(["iw", "dev"])
        if code == 0 and out:
            for line in out.split("\n"):
                line = line.strip()
                if line.startswith("Interface "):
                    wifi_ifaces.append(line.split()[1])

        code, out, _ = self._run_cmd(["ip", "-o", "link", "show"])
        if code == 0 and out:
            for line in out.split("\n"):
                if line.strip():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        ifname = parts[1].strip()
                        if "@" in ifname:
                            ifname = ifname.split("@")[0]
                        if ifname != "lo":
                            all_ifaces.append(ifname)

        return json.dumps({
            "wifi_interfaces": wifi_ifaces or ["wlan0"],
            "all_interfaces": list(set(all_ifaces)) or ["wlan0", "eth0"],
        })


_daemon_instance = None
_main_loop = None
_exit_status = 0


def on_bus_acquired(connection, name):
    global _daemon_instance
    print(f"[*] D-Bus system bus acquired: {name}")
    node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    interface_info = node_info.interfaces[0]

    daemon = WifiHotspotDaemon(connection)
    _daemon_instance = daemon

    connection.register_object(
        OBJECT_PATH,
        interface_info,
        daemon.handle_method_call,
        None,
        None,
    )


def on_name_acquired(connection, name):
    print(f"[*] Name successfully acquired on D-Bus: {name}")


def on_name_lost(connection, name):
    global _exit_status
    _exit_status = 1
    print(f"[!] Name lost on D-Bus: {name}. Exiting.")
    if _daemon_instance:
        _daemon_instance.request_shutdown(_main_loop.quit)
    elif _main_loop:
        _main_loop.quit()


def main():
    global _main_loop
    loop = GLib.MainLoop()
    _main_loop = loop
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        BUS_NAME,
        Gio.BusNameOwnerFlags.REPLACE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )

    def shutdown(signum, frame):
        if _daemon_instance:
            _daemon_instance.request_shutdown(loop.quit)
        else:
            loop.quit()
    signal.signal(signal.SIGTERM, shutdown)
    try:
        print("[*] WifiHotspot D-Bus service loop running...")
        loop.run()
    except KeyboardInterrupt:
        print("\n[*] Exiting daemon...")
        loop.quit()
    finally:
        if _daemon_instance:
            if not getattr(_daemon_instance, '_shutdown_complete', False):
                operations = _daemon_instance._operations
                operations.cancelled.set()
                try:
                    operations.executor.submit(_daemon_instance.method_stop).result(timeout=45)
                finally:
                    operations.close()
    return _exit_status


if __name__ == "__main__":
    sys.exit(main())
