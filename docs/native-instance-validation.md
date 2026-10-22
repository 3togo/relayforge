# Native instance options and housekeeping validation

Validated on 2026-10-22 as an ordinary user in the separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, reporting development branch), x86_64,
Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `47e153614d82976a150104427d44607c00fe715d`
- Source tree: `6ca3b694f754988f2fd60cb234250bd36d700b96`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service and native helper SHA-256: `5072a9fa918f3a7c9f5ccf68ad0bee438d6548615fa1739cacc49bdd464f2cfc`
- create_ap staging manifest SHA-256: `331a65266826d73c9b4e35d69aefeeabd2e3cae9cb779f1a2e84099d8526633d`
- NetworkManager staging manifest SHA-256: `db005854f10b7f8ceeaaed2e73070c7ae44f12a23d543a4cbc52211513b3a6d0`

The remote checkout matched this exact revision with no tracked differences or
extra untracked source before final validation and remained clean afterward.
The subsequent documentation commit records results only. Dependencies and
Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 41 passed |
| Full Python suite against optimized service and examples | 176 passed |
| Clippy with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Seven and six files, plus manifests |
| Native helper/service byte comparison | Identical ELF executables |
| Test observation shim syntax | Passed |

All 217 distinct test methods passed. The new Rust unit method validates the
internal option protocol and failure diagnostics. Eleven direct instance CLI
methods cover unknown/repeated/missing options, frozen-plan/path/permission
checks, explicit credentials, literal Unicode and shell-like configuration,
private markers, live-owner refusal, SIGUSR1/TERM/INT/USR2, killed-instance
recovery, allocation collisions, copied-directory rejection, modified markers,
foreign files, symlinks, hardlinks and occupied control directories.

Conditional transaction cases cover DNS/DHCP disabling, no Internet sharing,
and restoration of a simulated dedicated adapter and its NetworkManager state.
The ELF execution gate is tested against a private executable copy: absent
acknowledgement cannot execute it, and acknowledged production entry refuses
an ordinary user even when a fixture environment variable is supplied.

Existing desktop Start/Stop, asynchronous clients/signals, authorization,
supervisor-death, cancellation, recovery and cleanup-retry cases pass. The native
desktop fixture now delegates its lifecycle to the Rust instance CLI; its small
test-only Bash shim supplies simulated station observations. Direct instance
tests run Rust without that shim. Historical Bash protocol/parity tests remain,
but the generated adapter is never included in new staging.

## Evidence and limits

Ignored build artifacts retain `artifacts/validation-47e1536.log`,
`artifacts/create-ap-stage-47e1536/` and `artifacts/nm-stage-47e1536/`.
The final log and both manifests are retained locally as well. New native
payloads contain no Python or Bash backend. Their existing helper path now
contains a second regular copy of the service ELF. See
[native instance behavior](native-instance.md) for the internal CLI, directory
publication, signal handling, ownership checks and housekeeping limits.

No packages were installed and no service was installed, enabled, loaded or
restarted. No physical hotspot was activated. Read-only local audits before and
after the work showed the Wi-Fi interface connected and the original installed
daemon retaining the same process; the original project checkout stayed clean.

Real drivers, hostapd sockets, dnsmasq privilege dropping, mandatory access-control
policy, DHCP/DNS/NAT connectivity and deployed upgrade/rollback remain separate
hardware/deployment gates. An interrupted unpublished directory or final record
deletion can retain incomplete evidence requiring administrator review. These
tests establish simulated lifecycle and desktop compatibility, not production
or hardware certification.
