# Native existing-bridge implementation and validation — 2026-10-23

This implements task 14's first bridge case: attach an owned virtual AP child to
an existing administrator bridge. It is experimental and requires the native
create_ap service flag `--allow-existing-bridge`. Packaged units omit that flag.
New bridge creation and upstream migration remain unsupported.

## Implemented behavior

- Preflight requires an active independent Linux bridge with an administrator-set
  MAC (`addr_assign_type=3`), a separate Wi-Fi parent and `NO_VIRT=0`. Failures are
  exposed through capabilities and Start before launching the helper.
- A private versioned journal binds the boot/network namespace, bridge and parent
  identities, PHY and owned child. Child identity and explicit attachment intent
  are durable before changing membership or launching hostapd.
- The final daemon exec gate, readiness and monitoring verify membership.
  Hostapd names the same frozen bridge. No DHCP/DNS daemon, address, route, NAT,
  forwarding lease, bridge creation/deletion or upstream NM unmanagement is
  acquired for this mode.
- Stop records intent before stopping owned daemons, then verifies/detaches and
  deletes only the owned AP child. Recovery still runs if the service opt-in is
  removed. Settings edits cannot redirect cleanup away from its frozen plan.
- Replaced links, foreign master/address changes, namespace/provider changes,
  corrupt journals and unexplained pre-Stop detach retain recovery evidence.
  A detach completed after durable owned-stop intent is accepted. Repeated
  cleanup is idempotent; a reappeared child is refused.

The contract and kernel/provider race limits are in `native-bridge-design.md`.
The ten-method, three-signal desktop D-Bus contract remains unchanged.

## Exact source and environment

Base revision: `57c9980084570668577228c324c7a8ea1842f73e`, plus the preceding
uncommitted parity and band-switch work. The two unrelated dirty deployment
implementation files were replaced with base bytes only in validation snapshots;
their local diff was preserved byte-for-byte.

Full-suite source-receipt SHA-256:
`0b9302130b1fb31c37a65c2fb51c4f44f3338cd5c183e061f0a7089047b08b04`.
Source-bound Cargo build identity:
`6b66b397112eb1aeee154ce1dfcc181225cb566a9a16e2c4c84259d8f957e936`.

Metadata and package lifecycle source-receipt SHA-256:
`97bf17c155d05c9eb194d220250544c62b30985aa1ccfd39b0546154e947a0d2`.
That snapshot has identical packaging inputs; its only implementation difference
from the final snapshot is the bridge link-parser extraction and two additional
Rust parser tests. This report and the final plan/matrix/design ledger edits were
written after the snapshot; no implementation changed afterward.

Verified test environment: Ubuntu 26.10 (Stonking Stingray), x86_64;
Rust/Cargo 1.97.1, GCC 15.3.0, Clang 22.1.8, Python 3.14.8,
iproute2 7.2.0, dpkg-deb 1.23.7, GTK3 3.24.52, GTK4 4.24.1 and
libadwaita 1.10.0. Builds/tests used an isolated source snapshot and fresh
source-bound release target; no host package installation was needed.

## Results

Full offline runner `offline-iqgv3tsr`: exit status 0,
`full_python_suite: true`; all 34 check stages passed.

| Check | Result |
| --- | --- |
| Locked offline release build, all workspace targets | Passed |
| Rust tests | 53 passed |
| Python suite | 304 passed across 27 modules, including 23 bridge cases |
| Desktop suite | 63 passed; Node controller harness passed |
| Applet harness | 17 optimized and 17 ASan/UBSan cases passed |
| Provenance | 48 vendor and 10 integration files verified |
| Package tests | 3 cases passed, both backends; policy/bridge docs included and opt-in flags absent from all service units |
| AppStream and both manpages | Passed |
| Isolated package lifecycle | Bundled-to-split and split-to-split upgrades/removal/purge passed |
| Actual release ELF package assembly | Base, optional tray and optional GNOME packages built; not installed |

Bridge cases cover all durable phases and detach/delete boundaries, lost command
replies, early child creation failures, hostapd exec failure, Stop before
readiness, sleep/resume, killed supervisor recovery without the opt-in, provider
replacement, active settings changes, invalid/replaced bridges and foreign
children. Rust tests reject malformed, ambiguous, replaced and oversized link
observations using a sanitized shape checked against read-only iproute2 output.
NAT/no-sharing, device ownership, firewall, leases, retirement, band switching,
configuration, desktop and deployment regressions also ran. No cases were skipped.

Diagnostic candidates exposed an older child-only recovery-record compatibility
regression and internal-helper flag interception; both were fixed before the
final passing run. An initial settings test expected active edits to be refused;
it was corrected to verify the existing supported behavior and frozen cleanup
ownership. Those diagnostic runs are not passing evidence.

Lifecycle checks used a disposable Ubuntu 26.10 container with network disabled,
a read-only root, no NET_ADMIN and private package roots. They used stand-in
executables to exercise file ownership/preferences, not real service activation.
The separate final packages contain the freshly built native release executable:
SHA-256 `ff7984445a6dbb853064f425ab02b7f8b45e6671177465b75ccc4732f4370267`
(11,248,800 bytes). The retained package receipt records all package hashes,
sizes and dependency fields.

## Limits and preservation

All mutation/recovery tests used simulated kernel state, private D-Bus providers
and disposable owned dummy daemons. No real bridge/port, route, address, firewall
or active Wi-Fi configuration was changed by this work. The lifecycle run's
before/after device inventories matched. The full runner's whole-device inventory
comparison returned false; its wrapper did not retain the two inventories, so
the differing device cannot be identified. A read-only followup confirmed the
remote Wi-Fi connected with its recorded profile activation predating the run;
the local active Wi-Fi also remained connected. This is a limitation of the
whole-host observation, not a passing equality check. No installed tray/networking
service was restarted, and no candidate was installed or published.

Real hostapd traffic, DHCP supplied by the administrator bridge, VLAN/STP behavior,
driver support and kernel/provider races still require an isolated system and
dedicated radio. The bridge must already use a fixed MAC. An unexplained detach
before durable Stop can require inspection, including a spontaneous hostapd exit
that removed membership. Names/identities are rechecked, but these checks do not
provide atomic exclusion against another administrator's changes. These results
do not establish hardware readiness or full bridge parity.

Raw logs, source manifests and package receipts are retained under ignored
`artifacts/native-bridge-validation-20261023/`. Machine-specific paths and access
details stay outside repository history. The original source repository, pinned
imports and unrelated deployment changes remain preserved. No commit or push was
performed for this implementation batch.
