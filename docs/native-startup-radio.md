# Native create_ap startup radio policy

The generated native adapter delegates its remaining radio capability, station
channel and driver decisions to Rust. The original Bash vendor snapshot and
compatibility backend are unchanged. Desktop D-Bus methods, signals, replies and
authorization remain unchanged.

`--create-ap-runtime radio-check DIR` reads the private recovery snapshot and
frozen configuration, then inspects the original parent interface with the
existing bounded, read-only `iw dev info`, `iw phy info` and `iw dev link`
operations. The core parser retains the observed PHY identity internally;
the desktop capability JSON is unchanged. Driver/module identity comes from
sysfs, with a driver-name fallback for built-in drivers without a module link.

Before interface creation, Rust confirms a live owner, exact recorded parent
ifindex/MAC, a permitted AP channel, the supported parent mode and concurrency
on a connected station. The frozen band/channel must match the current plan;
no fallback selects a different channel or switches the upstream. Disabled,
no-IR, DFS and unknown channel restrictions remain conservative refusals.

A single-link mode-0600 `radio.json` binds the owner, parent, PHY, driver,
validated frozen configuration hash and upstream identity hash. Configuration
and upstream SSID/BSSID/password values are not stored literally in this file.
Repeated checks cannot overwrite a changed baseline. The daemon runtime repeats
these checks before rendering configuration and before launching children;
a missing, unsafe, malformed or changed baseline blocks both operations.
This narrows startup races; it does not prevent a station roaming after a check.
The outer supervisor continues observing upstream identity after readiness.

The pinned engine's brcmfmac virtual-interface guard is retained as an explicit
refusal. Its legacy 8192c/d and 8723a driver overrides are declined because native
hostapd supports nl80211 only. RTL-prefixed drivers with WPA1 require an explicit
WPA2 configuration instead of silently rewriting security settings. Other
supported native security settings remain literal. A disconnected dedicated
brcmfmac adapter can use explicit NO_VIRT when the ordinary preflight permits it.

Bash no longer parses radio capabilities/link frequencies, synchronizes or falls
back channels, changes regulatory settings, or rewrites driver/security options
in native mode. Manual driver/country/MAC overrides outside the native desktop
schema are declined. Optional `nf_nat_pptp` loading has been removed: native NAT
does not promise PPTP helper behavior, and administrators retain responsibility
for optional kernel modules. No module-loading replacement runs in Rust.

Bash still owns option parsing, config-file loading, shared locking, instance
markers, stop/recovery dispatch and filesystem housekeeping. Native networking
transactions and daemon children are Rust-owned; removing the adapter entirely
requires migrating this remaining lifecycle protocol. Staging still includes
the generated adapter and never installs or activates a service.

Offline captures cover plan restrictions, driver refusals, roaming, parent/PHY
changes, unsafe files, dead owners and interrupted startup. Private-bus tests
exercise the unchanged desktop Start/Stop API. These results do not establish
driver behavior, hostapd control sockets, DHCP/DNS/NAT connectivity or deployed
upgrade/rollback. No hotspot activation is required for this extraction.
