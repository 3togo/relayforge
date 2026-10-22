# Rust AP setup and forwarding transactions

Native create_ap now delegates AP link/type/address setup, AP teardown and IPv4
forwarding leases to Rust. The desktop D-Bus methods, signals and authorization
remain compatible. Staging does not install or activate anything.

## Interface ownership

Rust saves a private, atomic `interface.json` before the first mutation. It binds
the baseline to the supervisor marker, process identity, interface index and MAC.
Setup targets the recorded AP, refuses the upstream interface, bridge mode,
connected stations and existing non-link-local addresses. The generated adapter
also preserves the parent station power-save setting. It never flushes
addresses or changes a MAC. A virtual AP must already have its planned MAC;
a driver fallback to another MAC fails closed.

The transaction lowers the link if needed, selects AP mode if needed, and adds
only the configured gateway /24. Hostapd brings the AP up. Each command uses a
validated argument vector, a minimal environment and a bounded child process.
A dedicated physical adapter retains its original mode/link state and link-local
addresses. Cleanup removes only the gateway added by this instance and restores
the recorded mode/link state. A virtual AP is deleted only after ownership checks.
Foreign addresses, station connections and reused identities block mutations.

Failures and lost replies retain intent. Stop retries reconcile observed state
before continuing. Successful restoration leaves a completion record until the
engine confirms all resources are gone. Locks serialize setup, removal and
verification, including helpers that outlive the requesting process. Restored
physical adapters may subsequently reconnect without a repeated Stop altering
their new configuration.

## Shared forwarding

A separate private `forwarding.json` and shared lock record the original global
`ip_forward`, all current per-interface forwarding flags and `all`/`default`.
Each participant has an owner-bound preparing/active/releasing lease. Intent is
persisted before enabling global forwarding, then the uplink flag. A final
release restores the global value before per-interface baselines; intermediate
releases preserve forwarding required by other recorded owners. Restoration
intent survives failures, including a lost reply after a global reset.

Changing `ip_forward` also resets other IPv4 configuration defaults, as described
in the [kernel sysctl documentation](https://docs.kernel.org/networking/ip-sysctl.html).
This journal restores forwarding flags only; it does not claim restoration of
all other IPv4 sysctls. Production activation still requires a controlled hardware
window and review of distribution-specific settings. The isolated simulator
models the forwarding reset to verify restore ordering and interrupted retries.

Interface identities are checked before restoration. Unknown/historical AP
directories and legacy common forwarding backups block shared changes. Use the
previous matching payload to Stop and confirm cleanup before upgrading an active
installation. Do not discard pending journals to force migration. Changed values
that conflict with the recorded transaction fail closed; this is not a general
lock against administrator changes to kernel settings.

## Remaining Bash and validation boundary

[Rust device ownership](native-device-ownership.md) now allocates/creates virtual
interfaces and releases/restores their NetworkManager managed state. The generated
adapter retains option parsing, compatibility checks, instance markers and optional
modules. [Rust daemon supervision](native-daemons.md) now renders and launches
hostapd/dnsmasq. Bridge sharing, band switching, COUNTRY overrides and
automatic resume remain unsupported in native create_ap mode. The immutable
vendor snapshot and compatibility backend are unchanged.

The generator checks the pinned source hash and extraction boundaries. Its
native output has no Bash networking `/proc/sys/net` accesses, Python helper or direct iptables
commands. Seven payload files are staged. The fixture path reads private JSON
and temporary interface metadata; missing state never falls back to live kernel
operations. Tests cover partial writes, lost replies, ownership conflicts,
shared leases, physical restoration and desktop cleanup/recovery. No live
network operation or deployment is part of these checks.

## Connected subnet collisions

Native create_ap now shares its IPv4 /24 overlap parser with the native
NetworkManager controller. It checks connected routes before creating the AP
and again before changing its type or assigning its address. Only an
identity-recorded AP's own routes are excluded on retry. Existing connected
networks, including more-specific host routes, prevent conflicting setup.
Malformed route inspection fails closed. These reads narrow the activation
window; they do not lock out external network changes after inspection.
