# Design checkpoint for remaining native features

This records the implementation boundaries for tasks 11–17 in the parity plan.
The first band-switch and existing-bridge implementations are experimental
opt-ins, with separate offline evidence. They do not authorize exercising these
operations on a live development host. Ordinary native Start continues to preserve upstream
connections and regulatory state.

## Upstream band switching

The reference `switch_band_and_reconnect` selects the strongest permitted BSSID
for the same SSID, activates the existing profile, and verifies the observed
connection. If NM chooses the old band, it temporarily changes the saved profile
band and restores that setting. Failure attempts to reactivate the original
BSSID. The native port must retain the useful explicit request without copying
best-effort restoration as a claim of successful recovery.

Required before native enablement:

1. A separate administrator opt-in and the existing explicit, Polkit-authorized
   `SwitchBandAndReconnect` request. No ordinary Start may implicitly reconnect.
2. Record the exact NM owner, device ifindex/MAC, original profile UUID/settings
   identity, original BSSID/frequency and original band restriction before any
   mutation. Never identify a profile by a display name or SSID alone.
3. Bound and validate scan results. Choose only a same-SSID, permitted-channel
   candidate; malformed results, missing candidates or provider changes fail
   before reconnection. Do not log secrets or obtain a new password unnecessarily.
4. A durable intent before activation/profile changes, and completion proof after
   each step. Profile updates need provider-supported comparison or a verified
   exclusive lease. External edits cannot be overwritten during restoration.
5. Cancellation, Stop, sleep and failed association restore the original profile
   and setting when ownership is proven. Crash recovery must distinguish an
   untouched profile, an owned temporary change, and foreign/unknown changes.
   Unknown state stays visible and requires review instead of a success reply.
6. Verify the actual device/profile/BSSID/frequency and AP permission after each
   activation. Do not treat a successful method return as proof of association.

Private-bus fixtures must cover rescans, missing candidates, stale replies,
provider replacement, original-profile edits, Stop/sleep at every stage, crashes
before and after mutation, rollback failures and credential-free diagnostics.
Real NM association and recovery then require an isolated radio bench.

### First native implementation: BSSID-only requests

The first implementation uses an existing saved profile and explicitly selected
scanned AP. It does not call profile Update/Save/GetSecrets, pin a temporary band,
change autoconnect, acquire credentials or create a checkpoint. Profiles already
pinning a band/BSSID/channel are refused. This retains a useful subset without
introducing temporary profile edits that could overwrite administrator changes.

The provider contract was checked against the primary NetworkManager reference:

- [ActivateConnection](https://www.networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html): request an existing profile, exact device and scanned AP; retain the returned ActiveConnection identity.
- [Wireless](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/gdbus-org.freedesktop.NetworkManager.Device.Wireless.html): RequestScan is followed by LastScan confirmation; enumerate GetAllAccessPoints and verify ActiveAccessPoint.
- [AccessPoint](https://www.networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.AccessPoint.html): use raw SSID bytes, BSSID, frequency, infrastructure mode, strength and boot-time LastSeen.
- [Settings.Connection](https://www.networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.Settings.Connection.html): GetSettings excludes secrets; the Update2 comparison option does not make ActivateConnection conditional on the device's current association.

Only fresh same-SSID candidates on current AP-permitted channels qualify. The
strongest qualifying candidate wins, with a deterministic BSSID tie-break. Lists,
SSID sizes, scans, calls and verification are bounded. NetworkManager and iw must
agree on the resulting BSSID, SSID and frequency; a method reply is insufficient.

The private runtime journal records the bus GUID and NM owner, device identity,
profile path/UUID and semantic settings fingerprint, original/target AP identities
and transaction phase. SSID content and credentials are not recorded. NM's
last-used timestamp and seen-BSSID metadata are excluded from the fingerprint;
other saved settings must remain unchanged.

| Durable phase | Recovery behavior |
| --- | --- |
| Prepared | Request acceptance is unknown; retain evidence and refuse blind reconnect |
| Switching, with returned activation identity | Restore only while the exact provider/device/profile and returned activation still match |
| RestorePrepared | Restoration acceptance is unknown; retain evidence, do not retry blindly |
| Restoring, with returned activation identity | Verify the original association; clear only after proof, without issuing another activation |

Stop/sleep cancel the request and wait for guarded restoration. External profile,
provider, device or activation changes retain the journal instead of overwriting
them. Pending evidence blocks Start/config edits and must also be resolved at
service startup, even when the opt-in has subsequently been removed.

The service flag `--allow-band-switch` is an experimental administrator opt-in,
appended to the native service command. It is absent from packaged service units.
Only an explicit authorized SwitchBandAndReconnect call can use it, and sharing
must be stopped first. Ordinary Start never changes the upstream.

There is no conditional activate-if-current operation in this provider contract.
Identity checks detect observed external changes but cannot exclude another
client acting between the final observation and activation. The opt-in therefore
requires a controlled session; hardware/provider concurrency qualification and
the original profile-edit fallback remain open. This work does not enable or
exercise the flag on the developer's active networking host.

## Bridge sharing

The reference reuses an existing bridge or creates a new bridge, unmanages the
upstream, moves its addresses/routes and changes bridge netfilter behavior.
Those operations directly affect upstream connectivity, so copying them behind
an ordinary Start would violate RelayForge's current behavior.

Implement two separate cases:

- Existing administrator bridge: prove the bridge identity and reuse it without
  taking ownership of it. Attach only the owned AP interface. Do not delete the
  bridge, move upstream addresses/routes, change its STP/forward-delay policy or
  run a competing DHCP server. Restore/detach only the owned AP child.
- New bridge with upstream migration: require a separate explicit opt-in and a
  controlled environment. Journal parent/bridge identities, previous master,
  link flags, addresses, routes, NM managed state and all acquired leases before
  mutation. Preserve ordering of subnet/default routes and restore exact owned
  changes; never flush unrelated routes or adopt a foreign bridge.

Both cases need hostapd bridge configuration, DNS/DHCP suppression appropriate to
L2 sharing, no NAT rules, ownership-aware cleanup and connected-subnet policy
appropriate to bridging rather than a blanket NAT preflight. Global bridge
netfilter settings may not be rewritten as a side effect. Interrupted setup and
teardown need proof-based retry through the existing runtime/retirement journals.
Tests must cover foreign bridge replacement, parent renames, external route/NM
changes, kernel errors, crashes and repeated cleanup before physical testing.

The first existing-bridge path is now implemented behind
`--allow-existing-bridge` for native create_ap. It supports only an active,
independent administrator bridge, a stable bridge MAC and an owned virtual AP
child. No local DHCP/DNS/NAT/forwarding leases or upstream migration are acquired.
The full transaction contract, recovery limits and implementation boundaries are
in `native-bridge-design.md`; physical qualification remains separate.

## Drivers, security and regulatory behavior

| Reference behavior | Native decision and implementation requirement |
| --- | --- |
| nl80211 | Existing supported path; retain identity/radio and hostapd readiness checks |
| brcmfmac virtual-interface restriction | Existing explicit refusal; dedicated NO_VIRT path needs physical coverage |
| 8192c/d and 8723a vendor hostapd driver | Do not silently select rtl871xdrv. Require an explicit supported driver contract, matching hostapd build and representative captures before enabling |
| RTL WPA1 rewrite | Do not silently weaken or rewrite security; preserve an explicit supported security choice or return a specific refusal |
| WPA3 transition with raw PSK | Refuse before native startup: SAE needs a password; do not drop SAE or reinterpret a raw PSK as a password |
| Country setting | Native create_ap accepts a read-only requirement matching the already provisioned radio domain; setting/changing the domain stays administrator-owned |
| Parent MAC override | Needs explicit opt-in, identity rebinding and restoration proof; generated MACs on owned AP children remain the ordinary path |
| Optional PPTP module loading | Administrator provisioning, not an implicit modprobe during hotspot activation |
| firewalld integration | Active firewalld refuses native local iptables setup with backend guidance; preserve manager policy and keep owned cleanup available; native firewalld mutation remains unimplemented |

Unavailable legacy drivers or patched hostapd builds are real support gaps, not
reasons to claim success from a fixture. Keep exact refusals and capability
guidance until the corresponding contract and hardware evidence exist.

The source audit and shared driver/security policy are recorded in
`native-driver-security-audit.md`. Driver restrictions now appear before helper
launch and are rechecked through final daemon exec. Security descriptions label
WPA2/WPA3 transition mode. Stop/recovery remains available after policy changes.

Task 17's read-only regulatory constraint, active-firewalld checks and optional
kernel/MAC decisions are detailed in `native-regulatory-firewall-policy.md`.

## Gate order

First finish offline transactions and fault tests. Next verify the supported
distribution/provider APIs and lifecycle in an isolated system environment.
Finally exercise representative adapters and client traffic without using the
developer's active Wi-Fi connection as a test fixture. Keep implementation tasks
12, 14 and 16 open until their code and acceptance checks actually exist.
