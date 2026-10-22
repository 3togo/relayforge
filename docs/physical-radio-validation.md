# Read-only physical radio validation

On 2026-10-22, the Rust diagnostic executable was checked against a physical
Intel Wireless 8265/8275 adapter while preserving its existing Wi-Fi connection.
The physical checks passed. Hotspot activation was deliberately not attempted:
the only adapter was associated on a channel excluded by the current backend's
no-IR policy, and the automatic Start plan required moving the upstream to
2.4 GHz. A second physical adapter is needed to test activation while keeping
this upstream connection unchanged with the current backend.

## Exact candidate and environments

- Source: `cd993a239845eb3bdeb7aead4444443ff3701574`
- Source tree: `302af887b84bb0571f888f02d5c1c50f774b2353`
- Optimized executable SHA-256:
  `7d3ee41d774e52feca3b47240286f1eeff1724f08170afedff048cf2ad74a99f`
- Cargo.lock SHA-256:
  `84b72fa4915a5f4bed40bc217cc6e4891592f7f27d0bacacb557770d6a86d81c`

The executable was built on Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, using Rust/Cargo 1.97.1. The build checkout was verified identical
to the committed source, with no extra untracked source files. Its locked offline
workspace/all-targets build, 32 Rust tests, 32 Python integration/parity tests,
rustfmt, Clippy with warnings denied, optimized build and all 48 vendor integrity
checks passed. All 32 Python tests also passed using the optimized executable.

The physical probe ran locally on Ubuntu 26.10 (Stonking Stingray, stable),
x86_64, kernel `7.3.0-9-generic`, iw 6.17, driver `iwlwifi`, firmware
`36.c8e8e144.0 8265-36.ucode`. The transferred executable's SHA-256 matched the
remote build exactly. No local compilation was required.

## Physical observations

The monitored probe ran from 11:56:07 to 11:56:15 UTC. All commands used ordinary
user permissions. The diagnostic modes exit before opening D-Bus or constructing
the networking worker; their only adapter operations are read-only iw queries.
The installed service was queried only through GetStatus and GetCapabilities.

| Check | Observed result |
| --- | --- |
| Five repeated native live capability inspections | Identical results |
| Native versus installed backend capability JSON | All ten fields matched |
| Current upstream | 5765 MHz, channel 153, 5 GHz |
| AP/STA concurrency | Reported supported |
| Permitted hotspot channels under current parser policy | 2.4 GHz channels 1–13; no 5 GHz channels |
| Current upstream channel permitted for AP | False: marked no IR |
| Automatic create_ap preflight | Plan requires switching upstream to 2.4 GHz; plan not applied |
| Explicit 5 GHz create_ap preflight | Rejected: no permitted channel |
| NetworkManager preflight | Rejected: current upstream channel not permitted |
| Gateway reachability during probe | 16/16 packets received; 0% loss |
| Association, SSID, BSSID and frequency | Unchanged |
| Active connection UUID and connected state | Unchanged |
| Saved profile UUID/type inventory | Unchanged |
| IP addresses and default route | Unchanged |
| Wireless interface identities/types | Unchanged |
| Carrier state and up/down/change counters | Unchanged |
| Regulatory output | Unchanged |
| Installed daemon PID/state and hotspot status | Unchanged; hotspot remained inactive |

Passive NetworkManager monitoring reported only that NetworkManager was running;
link/route/address and iw event logs contained no events. The before/after carrier
counters and association independently corroborate no observed disconnect during
this short check. These observations do not establish long-duration reliability.

No Start, Stop, band switch, scan, connection edit, firewall mutation, regulatory
change, service restart or installation was requested. Three synthetic private
configuration files used for preflight were removed. All probe monitors exited.
No Android device was connected; the unused ADB server started by the availability
check was stopped. No administrator authentication was needed.

## Evidence and limits

Raw captures contain upstream identifiers and remain private in the ignored
`artifacts/physical-readonly-20261022/` directory alongside the bench script,
executable, before/after snapshots, command results and build logs. The private
result JSON SHA-256 is
`5cd8c1e6f2b5280255d0281ed0bec0d7a1125ec71bc767d995b43459c8e587c9`.
SSID, BSSID, connection UUIDs and private addresses are omitted from this report.

This validates live Rust capability inspection and read-only planning on the
physical adapter. It does not validate AP creation, client association, DHCP/DNS,
NAT, throughput, Android connectivity, ownership recovery or end-to-end cleanup.
The replay/private-bus tests remain simulated activation tests. The existing
backend conservatively excludes no-IR channels; this result makes no broader
claim about every kernel-supported concurrent-AP mode. No regulatory restriction
was relaxed to make activation possible.
