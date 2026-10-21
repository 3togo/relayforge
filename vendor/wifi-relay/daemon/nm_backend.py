#!/usr/bin/env python3
"""Service-owned NetworkManager AP session and recovery of its owned resources."""
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
import uuid
try:
    import legacy_runtime
except ModuleNotFoundError:
    from daemon import legacy_runtime

ROOT = Path(__file__).resolve().parent
probe_path = ROOT / "tools/nm_ap_sta_probe.py"
if not probe_path.is_file():
    probe_path = ROOT.parent / "tools/nm_ap_sta_probe.py"
spec = importlib.util.spec_from_file_location("relay_nm_probe", probe_path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

STATE_FILE = Path("/run/wifi-relay/nm-owned.json")
SYS_NET = Path("/sys/class/net")
MAX_EVENT_BYTES = 65536
MAX_DRAIN_BYTES = 4 * MAX_EVENT_BYTES
STARTUP_TIMEOUT = 55
STOP_TIMEOUT = 40


def validate_ownership(data):
    allowed = {"interface", "ifindex", "mac", "profile_uuid", "profile_id"}
    if (not isinstance(data, dict) or set(data) - allowed
            or not isinstance(data.get("interface"), str)
            or not re.fullmatch(r"wrnm[0-9a-f]{6}", data["interface"])
            or type(data.get("ifindex")) is not int or data["ifindex"] <= 0
            or not isinstance(data.get("mac"), str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", data["mac"])):
        raise ValueError("Invalid ownership record.")
    if "profile_uuid" in data:
        try:
            uuid.UUID(data["profile_uuid"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid ownership record.") from exc
        if data.get("profile_id", "Wi-Fi Relay NM probe") not in ("Wi-Fi Relay NM probe", "Wi-Fi Relay Hotspot"):
            raise ValueError("Invalid ownership record.")
    elif "profile_id" in data:
        raise ValueError("Invalid ownership record.")
    return data


def validate_options(config):
    config = probe.daemon.validate_config(config)
    unsupported = [key for key in ("NO_VIRT", "NO_DNS", "NO_DNSMASQ", "ETC_HOSTS",
                                   "MAC_FILTER", "IEEE80211N", "IEEE80211AC", "IEEE80211AX")
                   if config[key] != "0"]
    if config["WPA_VERSION"] != "2":
        unsupported.append("WPA_VERSION (WPA2 required)")
    if config["SHARE_METHOD"] != "nat":
        unsupported.append("SHARE_METHOD (NAT required)")
    if config["DHCP_DNS"] != "gateway":
        unsupported.append("DHCP_DNS (gateway required)")
    if "COUNTRY" in config:
        unsupported.append("COUNTRY (use the system regulatory domain)")
    if unsupported:
        raise ValueError("NetworkManager backend does not support these overrides: " + ", ".join(unsupported))
    return config


def write_ownership(data):
    validate_ownership(data)
    STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE_FILE)


def recover():
    """Remove only the recorded UUID and interface with matching index/MAC."""
    if not STATE_FILE.exists():
        return
    data = json.loads(STATE_FILE.read_text())
    try:
        validate_ownership(data)
    except ValueError as exc:
        raise RuntimeError("Invalid owned-interface recovery record.") from exc
    iface = data.get("interface", "")
    if not re.fullmatch(r"wrnm[0-9a-f]{6}", iface):
        raise RuntimeError("Invalid owned-interface recovery record.")
    sysnet = SYS_NET / iface
    if sysnet.exists():
        if (int((sysnet / "ifindex").read_text()) != data.get("ifindex")
                or (sysnet / "address").read_text().strip().lower() != data.get("mac", "").lower()):
            raise RuntimeError("Interface identity changed; refusing to remove it.")
    profile = data.get("profile_uuid")
    if profile:
        uuid.UUID(profile)
        existing = probe.command(["nmcli", "-g", "UUID", "connection", "show"]).splitlines()
        if profile in existing:
            name = probe.command(["nmcli", "-g", "connection.id", "connection", "show", "uuid", profile])
            expected_name = data.get("profile_id", "Wi-Fi Relay NM probe")
            if name != expected_name or name not in ("Wi-Fi Relay NM probe", "Wi-Fi Relay Hotspot"):
                raise RuntimeError("Profile identity changed; refusing to remove it.")
            probe.command(["nmcli", "connection", "delete", "uuid", profile])
    if sysnet.exists():
        # NM profile deletion can remove the interface while command() waits.
        # Check again rather than deleting a subsequently reused interface name.
        if (int((sysnet / "ifindex").read_text()) != data.get("ifindex")
                or (sysnet / "address").read_text().strip().lower() != data.get("mac", "").lower()):
            raise RuntimeError("Interface identity changed; refusing to remove it.")
        probe.command(["iw", "dev", iface, "del"])
    STATE_FILE.unlink()


def recover_all():
    """Service teardown attempts both backends even if one recovery fails."""
    errors = []
    for cleanup in (recover, lambda: legacy_runtime.recover_owned(
            Path('/run/wifi-relay/create-ap.pid'), ROOT / 'create_ap')):
        try:
            cleanup()
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError('; '.join(errors))


class Backend:
    def __init__(self, *, recover_on_init=True):
        self.process = None
        self.buffer = b""
        self._protocol_error = False
        self._termination_deadline = None
        self._activation_deadline = None
        self._recovery_pending = True
        self._desired_config = None
        self._upstream_uuid = None
        self._resume_after = 0
        self._resume_attempts = 0
        self._candidate = None
        self._candidate_since = 0
        self._sleeping = False
        self.cancelled = lambda: False
        self.status = {"active": False, "backend": "networkmanager", "client_count": 0}
        if recover_on_init:
            try:
                self._recover_owned()
            except Exception as exc:
                self.status["error"] = str(exc)

    def _recover_owned(self):
        # A worker can exit while NetworkManager itself is restarting. Keep
        # the ownership record and retry through the service's normal polling.
        self._recovery_pending = True
        recover()
        self._recovery_pending = False

    def _drain(self):
        if self.process is None or self._protocol_error:
            return
        if len(self.buffer) >= MAX_EVENT_BYTES:
            self._invalid_event()
            return
        drained = 0
        # Yield to the service loop even when a worker continuously writes.
        while drained < MAX_DRAIN_BYTES:
            try:
                data = os.read(self.process.stdout.fileno(), MAX_EVENT_BYTES)
            except BlockingIOError:
                break
            if not data:
                break
            drained += len(data)
            self.buffer += data
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    if len(line) >= MAX_EVENT_BYTES:
                        raise ValueError("Oversized worker event")
                    event = json.loads(line)
                    self._validate_event(event)
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    self._invalid_event()
                    return
                if event["event"] == "stage":
                    stage = event["stage"]
                    self.status["iface"] = event["interface"]
                    if "band" in event:
                        self.status["band"] = event["band"]
                    self.status["state"] = ("active" if stage == "active" else
                                            "stopping" if stage == "stopping" else
                                            "off" if stage in ("failed", "passed", "stopped") else "connecting")
                    self.status["active"] = stage == "active"
                elif event["event"] == "clients":
                    self.status["client_count"] = event["authorized_clients"]
                elif event["event"] == "result":
                    result = event["result"]
                    if result.get("user_disconnected") is True:
                        self._desired_config = None
                        self._upstream_uuid = None
                        self._candidate = None
                    self.status.update(active=False, state="off", client_count=0)
                    self.status["error"] = result.get("error") or "; ".join(result.get("cleanup_errors", []))
                elif event["event"] == "error":
                    self.status.update(active=False, state="off", client_count=0, error=event["message"])
            if len(self.buffer) >= MAX_EVENT_BYTES:
                self._invalid_event()
                return

    @staticmethod
    def _validate_event(event):
        if not isinstance(event, dict):
            raise ValueError("Invalid worker event")
        kind = event.get("event")
        if kind == "stage":
            if (event.get("stage") not in {"preparing", "native-interface-request", "interface-created", "waiting-for-device",
                    "restoring-ap-mode", "activating", "active", "stopping", "failed", "passed", "stopped"}
                    or not isinstance(event.get("interface"), str)
                    or not re.fullmatch(r"wrnm[0-9a-f]{6}", event["interface"])
                    or ("band" in event and event["band"] not in ("2.4", "5"))):
                raise ValueError("Invalid worker stage")
        elif kind == "clients":
            if type(event.get("authorized_clients")) is not int or event["authorized_clients"] < 0:
                raise ValueError("Invalid worker client count")
        elif kind == "result":
            result = event.get("result")
            if (not isinstance(result, dict) or not isinstance(result.get("cleanup_errors", []), list)
                    or any(not isinstance(item, str) for item in result.get("cleanup_errors", []))
                    or (result.get("error") is not None and not isinstance(result["error"], str))
                    or ("user_disconnected" in result and type(result["user_disconnected"]) is not bool)):
                raise ValueError("Invalid worker result")
        elif kind != "error" or not isinstance(event.get("message"), str):
            raise ValueError("Invalid worker event")

    def _invalid_event(self):
        self._protocol_error = True
        self.buffer = b""
        self.status.update(active=False, state="stopping", client_count=0, error="Invalid worker event.")
        if self.process.poll() is None:
            self.process.terminate()
            self._termination_deadline = time.monotonic() + STOP_TIMEOUT

    def get_status(self):
        self._drain()
        if self.status.get("active"):
            self._activation_deadline = None
        if (self.process and self._activation_deadline is not None
                and time.monotonic() >= self._activation_deadline):
            self._activation_deadline = None
            self.status.update(active=False, state="stopping", client_count=0,
                               error="NetworkManager hotspot startup timed out.")
            self._protocol_error = True
            if self.process.poll() is None:
                self.process.terminate()
                self._termination_deadline = time.monotonic() + STOP_TIMEOUT
        if (self.process and self._termination_deadline is not None
                and time.monotonic() >= self._termination_deadline
                and self.process.poll() is None):
            # Protocol failures must not strand a worker that ignores SIGTERM.
            self.process.kill()
            self._termination_deadline = None
        if self.process and self.process.poll() is not None:
            self.status.update(active=False, state="off", client_count=0)
            if self.process.returncode and not self.status.get("error"):
                self.status["error"] = "NetworkManager worker exited unexpectedly."
            self.process.stdout.close()
            self.process = None
            self._termination_deadline = None
            self._activation_deadline = None
            self._recovery_pending = True
        if self.process is None and self._recovery_pending:
            try:
                self._recover_owned()
            except Exception as exc:
                self.status["error"] = str(exc)
        status = dict(self.status)
        status["desired_active"] = self._desired_config is not None
        if self._desired_config is not None and self.process is None:
            status["state"] = "waiting"
        return status

    def start(self, config, *, automatic=False):
        if self.cancelled():
            raise RuntimeError('Hotspot operation cancelled.')
        config = validate_options(config)
        if self.process:
            raise RuntimeError("A NetworkManager session is already running or stopping.")
        self._recover_owned()
        upstream = None if automatic else probe.upstream_snapshot(config["WIFI_IFACE"])
        self.status = {"active": False, "state": "connecting", "backend": "networkmanager",
                       "ssid": config["SSID"], "phy_iface": config["WIFI_IFACE"], "client_count": 0}
        self.buffer = b""
        self._protocol_error = False
        self._termination_deadline = None
        self._activation_deadline = None
        try:
            self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            # Credentials travel only through an anonymous pipe, not argv or disk.
            os.set_blocking(self.process.stdout.fileno(), False)
            try:
                payload = dict(config)
                payload["EXPECTED_UPSTREAM_UUID"] = self._upstream_uuid if automatic else upstream[0]
                self.process.stdin.write(json.dumps(payload).encode())
            finally:
                self.process.stdin.close()
            if automatic:
                self._activation_deadline = time.monotonic() + STARTUP_TIMEOUT
                return self.get_status()
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while self.process and time.monotonic() < deadline:
                if self.cancelled():
                    raise RuntimeError('Hotspot operation cancelled.')
                status = self.get_status()
                if status["active"]:
                    self._desired_config = dict(config)
                    self._upstream_uuid = upstream[0]
                    self._resume_attempts = 0
                    return self.get_status()
                if self.process:
                    select.select([self.process.stdout], [], [], 0.1)
            raise RuntimeError(self.status.get("error") or "NetworkManager hotspot startup timed out.")
        except Exception as exc:
            error = str(exc)
            try:
                self.stop(preserve_intent=automatic)
            except Exception as cleanup:
                error += "; cleanup: " + str(cleanup)
            self.status.update(active=False, state="off", client_count=0, error=error)
            raise RuntimeError(error) from exc

    def stop(self, *, preserve_intent=False):
        self._activation_deadline = None
        if not preserve_intent:
            if self._desired_config is not None:
                print("Wi-Fi Relay: sharing request cancelled.", flush=True)
            self._desired_config = None
            self._upstream_uuid = None
            self._candidate = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.get_status()
        self._recover_owned()
        self.status.update(active=False, state="off", client_count=0)
        return True

    def prepare_for_sleep(self, sleeping):
        self._sleeping = bool(sleeping)
        self._candidate = None
        self._resume_after = time.monotonic() + 4
        if sleeping and self.process and self.process.poll() is None:
            self._activation_deadline = None
            self.process.terminate()
            self._termination_deadline = time.monotonic() + STOP_TIMEOUT

    def poll(self, config):
        """Resume only previously authorized sharing, never from a status read."""
        status = self.get_status()
        if self._desired_config is None or self._sleeping:
            return status
        if probe.daemon.validate_config(config) != self._desired_config:
            # Settings changed while waiting: require a fresh explicit Start.
            print("Wi-Fi Relay: settings changed; cancelling recovery.", flush=True)
            self.stop()
            return self.get_status()
        if self.process or self._recovery_pending:
            if status["active"]:
                self._resume_attempts = 0
            return status
        now = time.monotonic()
        if now < self._resume_after:
            return status
        try:
            upstream = probe.upstream_snapshot(config["WIFI_IFACE"])
            if upstream[0] != self._upstream_uuid or not upstream[1]:
                self._candidate = None
                self.status["error"] = "Waiting for the original Wi-Fi connection."
                return self.get_status()
            if upstream != self._candidate:
                self._candidate = upstream
                self._candidate_since = now
                return status
            if now - self._candidate_since < 4:
                return status
            report = probe.inspect(config["WIFI_IFACE"], "wrnm" + probe.secrets.token_hex(3))
            if not report["eligible_for_live_probe"]:
                raise RuntimeError("; ".join(report["blockers"]))
            self._resume_attempts += 1
            self._resume_after = now + min(60, 2 ** min(self._resume_attempts, 6))
            self.start(config, automatic=True)
        except Exception as exc:
            self.status["error"] = str(exc)
            self._resume_after = now + 10
        return self.get_status()


def worker():
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)

    def emit(event):
        nonlocal stopping
        try:
            print(json.dumps(event), flush=True)
        except BrokenPipeError:
            stopping = True

    try:
        payload = json.load(sys.stdin)
        expected = payload.pop("EXPECTED_UPSTREAM_UUID", None) if isinstance(payload, dict) else None
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", expected)):
            raise ValueError("Invalid expected upstream profile.")
        config = validate_options(payload)
        report = probe.inspect(config["WIFI_IFACE"], "wrnm" + probe.secrets.token_hex(3))
        if config["FREQ_BAND"] not in ("auto", report["band"]):
            raise ValueError("NetworkManager sharing uses the upstream band; reconnect Wi-Fi to the desired band first.")
        if config["CHANNEL"] != "default" and int(config["CHANNEL"]) != report["channel"]:
            raise ValueError("Concurrent sharing must use the upstream Wi-Fi channel.")
        result = probe.run_probe(report, config["SSID"], None, restore_ap=True,
                                 service_config=config, stop_requested=lambda: stopping,
                                 event_callback=emit, ownership_callback=write_ownership,
                                 expected_upstream_uuid=expected)
        emit({"event": "result", "result": result})
        if result["cleanup_errors"]:
            return 1
        recover()
        return 0
    except probe.ProbeFailure as exc:
        emit({"event": "result", "result": exc.result})
        return 1
    except Exception as exc:
        emit({"event": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        sys.exit(worker())
    if sys.argv[1:] == ["--recover"]:
        recover()
    elif sys.argv[1:] == ["--recover-all"]:
        recover_all()
    else:
        sys.exit("This helper is controlled by the Wi-Fi Relay service.")
