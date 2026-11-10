# Native driver/security validation — 2026-10-23

This continues tasks 15–16. The audit is in `native-driver-security-audit.md`.
It adds shared early driver/security policy, explicit transition-mode capabilities
and Settings presentation, and cleanup regressions. Patched vendor hostapd drivers
remain unsupported. No real driver or client certification is claimed.

## Source and environment

Base commit: `57c9980084570668577228c324c7a8ea1842f73e`, plus the preceding
uncommitted parity, band-switch and existing-bridge work. Validation snapshots
substituted base bytes for the two unrelated dirty deployment implementation
files; the local deployment diff was preserved byte-for-byte.

Final source-receipt SHA-256:
`dca530204dd497442a3e0b19c24c053aa4389ea80a264168fe659329266063d8`.
Source-bound Cargo build identity:
`c398b1ed03c36a039a0b72bbd443d08c0e7651a18d0d8dab5025ba645cdf8c54`.
This report and an audit clarification of the unchanged NetworkManager contract
were added after the source snapshot; implementation and other tested input bytes
remain unchanged. Candidate packages contain the snapshotted audit text.

Verified environment: Ubuntu 26.10 (Stonking Stingray), x86_64;
Rust/Cargo 1.97.1, GCC 15.3.0, Clang 22.1.8, Python 3.14.8,
dpkg-deb 1.23.7; GTK3 3.24.52, GTK4 4.24.1 and libadwaita 1.10.0 runtime
packages. Builds used an isolated snapshot and fresh source-bound release target.
No host packages or services were changed.

## Checks

An earlier focused run passed with source-receipt SHA-256
`36d7ff95882ecc33272d7019e35c8dee8a82ab5092f785a0c83a95403755e867`:
Rust, create_ap, radio-plan, daemon, bridge, desktop and provenance checks.
The final candidate additionally includes the instance input gate, a cleanup
regression, Settings label coverage, packaged audit and documentation.

Full offline runner `offline-fwnwq4w8` exited 0 with
`full_python_suite: true`; all 34 check stages passed.

| Check | Result |
| --- | --- |
| Locked offline workspace release build, all targets | Passed |
| Rust | 57 tests passed |
| Python | 311 tests across 27 modules passed |
| Desktop | 64 tests and Node controller harness passed |
| Applet | 17 optimized and 17 ASan/UBSan cases passed |
| Provenance | 48 vendor and 10 integration files verified |
| Both-backend Debian packaging | 3 cases passed, including installed audit/policy and absent experimental opt-ins |
| Actual ELF package assembly | Base, tray and GNOME packages built; not installed |

No cases were skipped. This adds four Rust policy tests, seven Python cases and
one desktop case to the preceding bridge batch; subcases exercise additional
driver/security and observation variants.

New cases cover exact vendor module refusals, dedicated brcmfmac, nl80211 module
profiles, malformed/missing/public/symlinked driver observations, changes between
capability inspection and Start, raw-PSK transition refusal, credential-free
capability descriptions, explicit protocol/PMF selection and Settings labels.
The cleanup regression starts owned dummy daemons, changes both driver and
security policy, then proves Stop removes only owned resources. Existing runtime
tests also cover driver/baseline replacement, crashes, failed launches and bridge
recovery without the experimental opt-in.

## Candidate packages

The final release ELF was assembled into create_ap base, optional tray and optional
GNOME packages (`0.1.0~driver1`); none was installed. Binary SHA-256:
`5d6b41259474e564ce23b369214bb3c27706eef39eb2b478b9c8326353beb603`
(11,200,176 bytes). The package receipt retains package hashes, sizes and
dependencies. Both-backend package tests cover the installed audit and policy,
and ensure experimental opt-ins remain absent from packaged units. No package
lifecycle requalification is claimed for this batch; the prior isolated lifecycle
evidence is recorded in `native-bridge-validation.md`.

## Limits and preservation

Mutation tests used simulated kernel files, private D-Bus providers and disposable
dummy daemon processes. Real interfaces, saved profiles, addresses, routes,
firewalls and regulatory settings were not changed by this work. The runner
retains separate before/after device inventories and active-profile timestamps.
The full-device equality guard returned false: one externally managed bridge and
one unmanaged Ethernet device disappeared, with no new devices. The retained
snapshots prove Wi-Fi device state and recorded profile activation unchanged;
Wi-Fi stayed connected. Whole-host inventory stability is not claimed. The local
active Wi-Fi also remained connected. No installed networking service or tray was
restarted.

Fixture module names exercise policy and recovery; they are not representative
hardware captures. Hostapd is not installed on the test host, so its compiled
driver/SAE support and actual client negotiation remain unverified. General
nl80211 support is still conditional on actual AP/radio observations. No automatic
driver substitution, WPA downgrade, profile rewrite or global networking change
was introduced. Dedicated-adapter traffic, vendor contracts and task 17's
administrator-owned regulatory/firewall integration remain open.

Ignored `artifacts/native-driver-security-validation-20261023/` retains raw logs,
source receipts, network observations and package metadata. Machine-specific
operational records stay outside repository history. Pinned imports, original
sources and unrelated deployment work were preserved. No commit or push was made.
