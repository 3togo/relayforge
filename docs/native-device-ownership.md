# Rust interface creation and NetworkManager ownership

Native create_ap now delegates interface allocation/creation and per-device
NetworkManager managed-state handling to Rust. AP setup/teardown, forwarding,
firewall rules, process supervision and recovery remain Rust transactions. The
original desktop D-Bus contract and pinned vendor source are unchanged.

## Creation and recovery

Rust chooses a short random `rfap…` name and a locally administered unicast MAC.
It checks current netdev and iw-reported addresses, including P2P reservations,
before saving the planned identity. The per-instance network lock and shared
allocation lock exclude overlapping helpers. No common Bash name-reservation
files or global interface counters are needed.

The validated recovery snapshot and private atomic `device.json` exist before
`iw dev PARENT interface add NAME type __ap addr MAC` runs. There is no fallback
that creates an interface without the planned MAC. Rust checks parent index/MAC
and PHY before creation, then records the resulting AP index/MAC/PHY. Each
command uses structured arguments, a minimal environment and bounded execution.
The upstream link, addresses, power-save setting and NetworkManager managed
state are never written by this transaction.

A failed or lost creation reply retains intent. Cleanup may reconcile a child
only when its planned name, MAC, radio and parent identity match. Renamed,
reused or changed devices retain evidence for review; recovery never deletes an
arbitrary interface found by MAC. Missing private fixture data cannot fall back
to real kernel operations. Physical dedicated mode records the existing adapter
and does not create an additional interface.

`interface-create DIR` returns only validated, quoted shell assignments. Cleanup
refreshes those fields from the snapshot with `device-fields DIR`, so a crash or
failed shell acknowledgement cannot leave cleanup using stale parent variables.
The snapshot continues to use the existing recovery schema and contains no
credentials.

## NetworkManager release and restoration

`device-release DIR` discovers the service's unique bus owner and exact device
object path, reads Managed/State/ActiveConnection, then saves the original
managed flag before changing it. It accepts only inactive devices and never
calls Disconnect, activates a profile or sets an upstream property. A running
manager must discover the AP; a failed query is not treated as an absent service.
The transaction confirms unmanaged state before allowing AP configuration.

A newly created virtual AP is explicitly set unmanaged even if its first
observation already says `Managed=false`. NetworkManager can publish that value
during discovery, before automatically managing the device. Observing it alone
does not establish a handoff. Existing unmanaged physical adapters retain their
policy without an unnecessary write.

The implementation uses the established writable `Managed` property to retain
compatibility with older NetworkManager releases. Its changes are nonpersistent;
NetworkManager 1.58 deprecates that setter in favor of the newer flag-bearing
SetManaged method. See the [official device API](https://www.networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.Device.html).
This extraction does not request persistent managed-state changes or edit
NetworkManager.conf. Native `--fix-unmanaged` is declined instead of performing
a global repair or signalling the service.

Before configuration and removal Rust rechecks ownership and active state.
A new activation, changed manager owner/path or changed kernel identity blocks
mutations. Cleanup deletes a virtual AP while it is still released from
NetworkManager, then confirms its disappearance. There is no managed=yes write
to a virtual interface about to be deleted. A dedicated adapter first restores
its original interface mode/link state, then restores managed=yes only if this
transaction originally changed that flag. An originally unmanaged device stays
unmanaged. Retries reconcile lost replies and preserve any later connection once
restoration has already succeeded.

`device-restore DIR` records completion; final cleanup refuses incomplete device
ownership. NetworkManager restarts, unresolved historical shell ownership and
identity conflicts retain journals for a Stop retry/operator review rather than
adopting another service's state. Stop the previous matching payload and confirm
cleanup before upgrading an active installation.

## Remaining boundary

The [Rust instance runner](native-instance.md) now parses options, manages instance
markers and dispatches transactions. New native staging contains an ELF helper;
optional module loading is removed. [Rust daemon supervision](native-daemons.md)
now renders hostapd/dnsmasq configuration and launches their foreground processes. The native runtime
has no Bash allocation functions, unmanaged-device helpers, nmcli calls,
interface-add commands, networking sysctl accesses or direct iptables commands.
It stages seven payload files without changing system activation.

Isolated tests use JSON kernel fixtures and a private-session-bus NM property
service. They exercise the actual Rust D-Bus wire calls, denial/timeouts, lost
replies, manager restart, state restoration, generated shell assignments, desktop
startup failure and supervisor crash recovery. These checks do not establish
live driver/udev behavior, actual NetworkManager policy, DHCP/DNS/NAT, or deployed
upgrade/rollback. No physical activation is part of this extraction.
