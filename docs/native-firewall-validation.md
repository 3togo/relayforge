# Native firewall extraction validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8
and systemd 261.3. Builds, automated checks and staging ran as an ordinary user
in the separate remote development checkout. No test invoked the actual system
firewall or activated sharing. The laptop's Wi-Fi and installed daemon were left running.

## Exact candidate

- Implementation: `4c0b45ec56fb06ec7f85ac22721a96e5cd5e4d23`
- Source tree: `fc1d92238c6f7a44a024616c90ab0399ef27451c`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `06350777320ddc6358671592aba0e6461b7fdd3aaf957fb461426f1fe0976d51`
- Native Bash adapter SHA-256: `de30c37a2bf9f1758325645cb488b9159aaa02c5539bbaf33ed55102c058e193`
- create_ap staging manifest SHA-256: `00323c04af68b99ec6c802b439c957c34e21c97e98fc350d421c8a58b16eb583`
- NetworkManager staging manifest SHA-256: `848eb3fe80559aa826f1109005bc1f5c989d716040a8c6ce0e44efc3ddae181b`

Git verified the exact source revision, identical tracked content, no extra
untracked source and a clean build checkout before the optimized checks. The
subsequent documentation commit only records results and links this report.
Cargo.lock and dependency versions are unchanged by this extraction.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 39 passed |
| Full Python suite against optimized service, parity and child executables | 96 passed |
| rustfmt and Clippy with warnings denied | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap staging | Seven files plus manifest; no Python files or direct Bash iptables calls |
| NetworkManager staging | Six files plus manifest |
| Generated Bash syntax | Passed |
| Both alternative service units, separate static verification | Passed |

There are 135 distinct test methods. Thirteen new firewall methods include
additional failure subcases: parity with all eight pinned Bash insertion rules,
idempotency, exact foreign-marker preservation, partial insertion at each of
eight positions, lost insertion/deletion replies, failed deletion and retry,
known duplicate cleanup, unknown exactly tagged rules, failed/malformed listings,
failed presence checks, DNS-disabled port-zero handling, unsupported phases,
transaction-lock contention, unsafe state/lock files, dead owners, reused AP
identities and bounded cleanup. Rule parity executes only extracted commands
with a printing function substituted for iptables; it never runs that program.

The non-networking lifecycle engine now applies and removes simulated firewall
rules through Rust. Tests exercise the unchanged desktop client, cleanup retry
after a firewall failure, journal retention and recovery of live children/rules
after supervisor death. Foreign rules survive these lifecycle checks. Rust unit
coverage includes literal quoted listing parsing, exact comment comparison and
structured process exit codes with discarded stderr. Existing configuration,
authorization, radio, NetworkManager, recovery and compatibility tests still pass.

## Evidence and limits

The build checkout retains `artifacts/validation-4c0b45e.log`,
`artifacts/create-ap-stage-4c0b45e/` and `artifacts/nm-stage-4c0b45e/`. The final
log and manifests are also retained locally under ignored artifacts. Static unit
verification used separate temporary copies with ExecStart pointing to the built
executable; only an unrelated installed-unit legacy PIDFile warning was emitted.
No unit was loaded or activated, and no staged files were installed.

Rust now owns the native create_ap NAT/DNS/DHCP rule plan, execution, removal and
absence checks. This does not complete the whole Bash replacement: interface
transactions, forwarding sysctl backup/restore, daemon configuration/launch and
optional module loading remain in the adapter. See [the firewall guide](native-firewall.md).

The fixtures do not establish real iptables/kernel behavior, distribution firewall
compatibility, driver behavior, DHCP/DNS/NAT connectivity, installation, rollback
or suspend/resume. The real Bash networking path was not activated. Read-only
checks still showed the laptop connected and the installed daemon running with
its original process identity. The original source checkout and current network
and service configuration were unchanged.
