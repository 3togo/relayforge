# Native regulatory/firewall validation — 2026-10-23

This implements task 17's read-only country requirement and active-firewalld
guards. The full behavior and remaining administrator-owned variants are in
`native-regulatory-firewall-policy.md`. This is offline implementation evidence,
not regulatory approval, traffic certification or native firewalld integration.

## Exact source

Base commit: `57c9980084570668577228c324c7a8ea1842f73e`, plus preceding
uncommitted parity/bridge/driver-security work. Both unrelated dirty deployment
implementation files were substituted with base bytes only in validation
snapshots. Their local diff remained byte-for-byte unchanged.

Final source-receipt SHA-256:
`d445e04857922b6d3be00e7628eba088d19c3ee019e591795dba0b89e6a235b7`.
Cargo build identity:
`a2727b0a16f72faadf10341f3ef3c5557e561c1d78ad8620ac4ccc34462241ab`.
This report was updated after the final snapshot; implementation, policy
documentation, plan ledger and other tested input files remain unchanged.
The retained archive and per-file receipt identify the exact
candidate, including intended untracked files and excluding unrelated edits.

Verified remote environment: Ubuntu 26.10 (Stonking Stingray), x86_64,
Rust/Cargo 1.97.1, Python 3.14.8 (GCC 15.3.0) and iw 6.17.
Builds use an isolated snapshot and a fresh source-bound release target.

## Validation

An initial existing-suite run passed with receipt SHA-256
`44d0507a2ab2b9f55acd9d329280bcecce30fea7d8dd11729bfc9d1778c0aef6`.
A subsequent focused candidate exposed an overbroad test assertion that assumed
native engine bookkeeping must disappear. The corrected test verifies owned
process death, instance retirement, supervisor-journal removal, preserved foreign
rules and restored simulated interface/forwarding state. Policy fixture updates
are atomic. No implementation changed to bypass this test failure.

The pre-guard candidate with receipt
`10f22abffc4a4d9ba6fa5f365bb9639cd6aad2c196064fd63e27795a21060b3d`
also passed the full suite with unchanged device inventories. Final review then
added the production native-ELF requirement at preflight and the last execution
gate, plus a unit test, so retained compatibility scripts cannot receive the
new COUNTRY contract. Final validation includes this additional guard.

Final focused runner `offline-iud4smq5` and full runner `offline-hc4hnlr1`
both exited 0. The full runner records `full_python_suite: true`; all 35 check
stages passed.

| Check | Result |
| --- | --- |
| Locked offline workspace release build, all targets | Passed |
| Rust | 60 passed |
| Python | 323 passed across 28 modules, including 12 new policy cases |
| Desktop | 65 passed and Node controller harness passed |
| Applet | 17 optimized and 17 ASan/UBSan cases passed |
| Provenance | 48 vendor and 10 integration files verified |
| Both-backend Debian packaging | 3 cases passed, including installed policy documentation |
| Actual release ELF package assembly | Base, tray and GNOME packages built; not installed |

No cases were skipped. The added coverage comprises three Rust tests, twelve
Python cases with additional subcases, and one Settings presentation case.

Candidate create_ap base, tray and GNOME packages (`0.1.0~policy1`) were assembled
with the final release ELF and not installed. Its SHA-256 is
`a13d7145b99a1596bf00bc48276b0331a89db9ef94a1dba8e2758f3559e3beae`
(11,320,544 bytes); the retained package receipt records all package hashes,
sizes and dependency fields. Package lifecycle was not requalified in this batch;
the earlier isolated file-lifecycle evidence remains in `native-bridge-validation.md`.

New cases cover matching/mismatched country, wrong/global/duplicate radio
observations, rule changes without a country change, baseline retention, daemon
rendering without country hints, missing/unsafe/corrupt observations, firewalld
before Start and between rule phases, late manager/domain changes while running,
bridge mode without local firewall acquisition, and killed-supervisor recovery
after country mismatch and firewalld activation. The latter proves startup policy
does not prevent cleanup of already owned resources. Existing failure, Stop,
sleep, firewall, driver/security and bridge recovery suites also run.

Read-only host probes confirmed per-radio `iw phy phyN reg get` support and the
system bus `NameHasOwner` query for firewalld, which reported no owner. Neither
probe changes policy or starts firewalld. Installed/active firewalld distributions
and actual packet behavior remain unqualified.

## Preservation and limits

All mutation/recovery tests use simulated kernel/firewall files, private D-Bus
providers and disposable dummy daemons. They do not call real `iw reg set`,
iptables mutation, modprobe or firewalld mutation APIs. No real hotspot, saved
profile, regulatory domain, firewall manager or upstream interface was changed
by this work. No networking service or tray was restarted and no package was
installed. Before/after device inventories and active-profile timestamps are
retained separately and matched exactly in the final run. Wi-Fi stayed connected;
the local active Wi-Fi also remained connected. The unrelated deployment diff
was verified unchanged after validation.

The country requirement checks a bounded observation and existing permitted
channel policy; it does not interpret regulatory rules as legal authorization.
Concurrent policy changes are not atomically excluded. The supervisor polls
about once per second after bounded inspection completes. Cleanup preserves
administrator country/firewall state rather than attempting to restore it.

The firewalld guard checks active D-Bus ownership, not arbitrary nftables/UFW or
other administrator firewall policy. Native firewalld rule management, global
country/parent-MAC changes, optional PPTP provisioning and real driver/client
traffic remain open. No legacy best-effort global changes were enabled.

Raw logs, source archives/receipts, package metadata and private observations are
retained under ignored `artifacts/native-regulatory-firewall-validation-20261023/`.
Machine-specific operational records stay outside repository history. Original
sources and pinned imports remain unchanged. No commit or push was performed.
