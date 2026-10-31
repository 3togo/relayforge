# Native bridge implementation contract

This records task 13's contract. The first implementation now supports the
existing-bridge case behind the explicit `--allow-existing-bridge` native
create_ap service opt-in; packaged units omit it. Recovery tests use simulated
interfaces only. Real bridge, route, address, service and firewall settings on
the development hosts remain untouched.

## First supported case

Reuse an existing administrator-managed Linux bridge and attach only a newly
owned virtual AP child. The selected `INTERNET_IFACE` must already be that bridge.
Require `NO_VIRT=0`, a permitted radio plan, no country/parent-MAC overrides, an
idle service and a separate explicit administrator bridge opt-in. Neither an
interface name nor a configured `BRIDGE_IFACE` string establishes ownership.

Before mutation, capture and validate the boot/network-namespace identity,
bridge ifindex/MAC and link kind, parent PHY/device identity, owned child token,
ifindex/MAC and its original absence of a master. Reject renamed/replaced links,
foreign children, an AP already attached to a master, a non-bridge upstream and
ambiguous provider observations. The bridge remains administrator-owned.

No upstream unmanagement, address/route movement, STP/forward-delay adjustment,
bridge creation/deletion, sysctl/module operations or firewall policy rewrite is
part of this case. New bridge creation and migration of an existing upstream are
a separate later transaction with their own opt-in and complete restore model.

## Changes required in the current runtime

| Boundary | Required change before bridge enablement |
| --- | --- |
| `relayforge-core/src/radio.rs` native preflight | Introduce a distinct validated existing-bridge plan; do not simply remove the current refusal |
| `ap_runtime/instance_runtime.rs` | Freeze the exact bridge plan in the instance before creating any interface; no NAT/subnet acquisition |
| `ap_runtime/network.rs` | Extend simulated and real observations with link kind/master; add versioned bridge-attachment intent and identity checks |
| `ap_runtime/daemons.rs` frozen/render/manifest paths | Accept only a frozen validated bridge plan, render hostapd `bridge=...`, and suppress dnsmasq/address/NAT/forwarding acquisition |
| daemon exec/readiness | Persist attachment intent before hostapd can attach its AP child; verify resulting bridge membership and AP readiness |
| teardown and retirement | Stop the owned daemon, verify/detach only the owned child as needed, and remove only that child; preserve the administrator bridge and all upstream state |
| backend/service/capabilities | Keep the default refusal; expose opt-in status and specific readiness/recovery errors; preserve the existing desktop D-Bus methods/signals |

Hostapd may attach the AP during startup. Its launch is therefore a mutation
boundary even if RelayForge does not issue a separate link-master command.
The journal must precede the launch, and recovery cannot rely only on the daemon
PID or a successful process exit. Existing NAT/no-sharing behavior must retain
its current frozen-schema and ownership checks.

## Attachment and cleanup states

1. **Prepared:** bridge/parent identities and child-creation intent are durable;
   no membership change is authorized for a foreign or already-mastered child.
2. **Child owned:** retain the exact created child's identity and original master
   absence before launching hostapd with the frozen bridge configuration.
3. **Attach requested:** persist before daemon exec; a lost reply/crash requires
   kernel membership observation, not an assumption that nothing happened.
4. **Attached:** require the recorded child and bridge identities and membership;
   only then report a ready hotspot. No local DHCP/NAT service is acquired.
5. **Detach requested:** persist before stopping a daemon that may change bridge
   membership. Re-observe after daemon exit; only the owned child's membership
   may be changed.
6. **Clean:** prove the owned daemon/child are absent and that no acquired lease
   or attachment remains before discarding recovery evidence.

An external master change, link replacement, unknown daemon launch or failed
membership observation retains evidence. Do not flush routes, delete a bridge,
detach unrelated ports or reinterpret a name match as ownership. Recovery must
handle both hostapd detaching automatically and the child still being attached.

## Offline acceptance before implementation closure

- Existing-bridge success records no address, route, NAT, forwarding, DHCP,
  bridge-create/delete or parent-managed-state mutation.
- Reject non-bridge/replaced/renamed upstreams, foreign/mastered children,
  NO_VIRT, incompatible radio plans and unsupported configuration before launch.
- Inject failure/crash before and after each journal, child-creation, daemon-exec,
  attach, readiness, stop, detach and delete boundary; repeat cleanup safely.
- Preserve evidence on foreign master/bridge/child changes and malformed or
  incomplete link observations; never affect an unowned bridge or port.
- Cover cancellation, Stop/sleep, process replacement, pending-recovery startup,
  provider replacement, concurrent settings changes and service-mode switching.
- Rerun NAT/no-sharing, daemon rendering, leases, firewall and retirement suites.

After offline acceptance, use an isolated network namespace/system environment
with a dedicated adapter and administrator bridge for traffic and recovery
checks. The developer's active Wi-Fi connection is not an acceptable fixture.
The existing-bridge implementation and offline fault tests are recorded in
`native-bridge-validation.md`. Traffic and hardware qualification remain open.

## Implementation notes

`ap_runtime/network/bridge.rs` now owns the bridge plan and attachment journal.
It binds boot/network-namespace, bridge and parent/PHY identities, then records
the created child's identity and an explicit attach authorization before the
membership command. Setup attaches the owned child before hostapd launch; the
hostapd configuration names the same bridge. The final daemon exec gate,
readiness and monitoring all recheck the attachment. Cleanup records intent
before stopping hostapd, accepts a detach completed by that owned stop, and
detaches/deletes only the recorded child.

An unexplained detach observed before Stop, an attachment without write-ahead
authorization, foreign master/address changes, replaced links, changed namespace
or provider ownership retain evidence. A spontaneous hostapd exit that already
removed membership before a durable stop intent may therefore need inspection;
cleanup does not guess whether another actor caused that change.

The bridge must already have an administrator-assigned MAC (`addr_assign_type=3`);
the preflight and recovery guard refuse automatic MAC selection. This avoids
the kernel recalculating bridge identity when ports change, as implemented in
[Linux bridge ID selection](https://raw.githubusercontent.com/torvalds/linux/master/net/bridge/br_stp_if.c).
No bridge MAC, STP,
forward-delay, VLAN, netfilter or upstream managed-state policy is changed.
The command boundary uses the documented child `master`/`nomaster` operations
from [ip-link](https://man7.org/linux/man-pages/man8/ip-link.8.html).
Names and identities are rechecked, but kernel/provider races and actual driver
behavior still require isolated system/hardware qualification.
