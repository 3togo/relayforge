# Native AP setup and forwarding validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.
Builds, automated checks and staging ran as an ordinary user in the separate
remote development checkout. No live interface, firewall or sysctl was changed.

## Exact candidate

- Implementation: `cb6c494258f340d67224e5102a23418dab06bccb`
- Source tree: `23a96fee7fa39c851ad398edce13a5d5990c3fce`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `17411cd717931485ec67d8e8261b5f0035a0fcdc3e0cd650a541392a456d5a0b`
- Native Bash adapter SHA-256: `a2527f5172e0cf782b67f2eb243d477e1b030e1b1aaf9a083d3f8a8f3cbc1b6e`
- create_ap staging manifest SHA-256: `1cf5227e1b1ba362fd32410619f3d575bdb0f09efcdb9f1d4b233635581d041c`
- NetworkManager staging manifest SHA-256: `8442ee002f76561d8dd6261c63af6206d1c05525ce9947713107edd0bd33aea3`

Git verified the exact revision, identical tracked content and no extra untracked
source before optimized validation. The subsequent documentation commit records
results only. Cargo.lock and dependency versions are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 39 passed |
| Full Python suite against optimized service, parity and child executables | 119 passed |
| Clippy with warnings denied and rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap staging | Seven files plus manifest |
| NetworkManager staging | Six files plus manifest |
| Generated Bash syntax | Passed |
| Both alternative service units, separate static verification | Passed |

There are 158 distinct test methods. Twenty-two new network methods include
additional subcases for failed writes and lost replies at each interface setup
and physical restoration step, dedicated-adapter restoration, virtual deletion,
changed identities, foreign addresses, connected stations, missing/unsafe state,
lock contention, early-start cleanup and a dead owner. Forwarding cases cover
mixed baselines, global-reset ordering, shared owners with one or two uplinks,
interrupted restoration, external changes and historical/foreign participants.
The new physical-restoration check requires completion evidence rather than
accepting a missing journal as proof.

The simulated lifecycle engine now invokes real Rust interface and forwarding
transactions alongside recovery/firewall operations. A new desktop integration
test forces persistent forwarding-write failure: Stop returns false and retains
its journals, then succeeds after the injected failure is removed. Existing
unchanged-client, cancellation, supervisor-death and recovery tests also pass.
No fixture can fall back to real networking utilities or sysctls.

The generator verifies the pinned source and extraction boundaries. Its output
has fourteen Rust runtime calls, no Python helper, no direct Bash iptables command,
no networking sysctl access and no parent station power-save change. The original
vendor source remains intact. The retained entropy availability read is unrelated
to network sysctls.

## Evidence and limits

The build checkout retains `artifacts/validation-cb6c494.log`,
`artifacts/create-ap-stage-cb6c494/` and `artifacts/nm-stage-cb6c494/`. The final log
and manifests are also retained locally under ignored artifacts. Static unit
verification used separate temporary copies with ExecStart pointing to the built
executable; an unrelated installed-unit legacy PIDFile warning was emitted.
No service was installed, loaded, enabled or restarted.

Rust now owns AP link/type/address setup, teardown and forwarding leases. Bash
still allocates/creates interfaces, handles NetworkManager unmanaged metadata,
configures/launches daemons and loads optional modules. Existing desktop controls
retain their contract. See [network transaction details](native-network-state.md).

Physical driver behavior, live sysctl semantics, DHCP/DNS/NAT connectivity and
deployed upgrade/rollback remain untested. Global `ip_forward` changes can reset
other IPv4 settings; the journal restores forwarding flags only. No hardware
activation was performed. The laptop's existing Wi-Fi remained connected and its
original installed daemon remained active with the same process throughout.
