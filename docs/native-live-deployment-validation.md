# Native live service handover validation

Validated on 2026-10-22 UTC using ordinary-user disposable image directories and
a private session bus on Ubuntu 26.10 (Stonking Stingray, stable), x86_64,
Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `f530c10a6e7b1e91ddf3553a235fa118f4422b10`
- Source tree: `49164080819115f6d29c7c071b63d01599c91a16`
- Cargo.lock SHA-256: `a3db3caf038220ff20b1c23ef3ee25742ff56bd983062e89df91e39f18ba20e4`
- Optimized service/helper SHA-256: `6b9a30d12685e106e69e9e0f73492793897f99a0ab81cf6ff7514ff5000bab42`
- create_ap manifest SHA-256: `91e0c6ea3b0730b2ef60b71b98cf2e376cdd568517bcb8e890864a9fee10e178`
- NetworkManager manifest SHA-256: `13fa13d7ce3b6fe75041378ec4fad8fb180c9e3e23f5aee1c38addc893bc1711`
- Validation log SHA-256: `f5dccfa527e64e9da10ad09e2ed843adf6c7ad6edd75eb1cbf393eff92357631`

The remote checkout matched this committed candidate and was clean before and
after the final run. Dependencies, Cargo.lock and the pinned desktop files are
unchanged. Concurrent local packaging edits were excluded from this candidate
and preserved. The following documentation commit records results only.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 46 passed |
| Full Python suite against optimized executable/examples | 239 passed, 271.429 seconds |
| Clippy all-targets with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |
| Explicit deployment protocol query | relayforge:live-maintenance:v1 |

All 285 test methods passed. Sixteen new live-deployment methods use actual
optimized Rust CLI invocations, real staged ELF copies, real Rust demo service
processes, kernel bus credentials/PIDs, /proc executable verification, descriptor-
relative filesystem transactions, durable receipts and flock. The systemd manager
itself is a Python private-bus fixture; no installed service manager is called.
D-Bus ReloadConfig exercises the actual disposable session bus.

Coverage includes fresh installs for both backends, upgrades, rollback to the
preceding native snapshot and empty baseline, repeated rollback, read-only plans,
configuration and desktop preservation, all exposed handover fault phases plus
selected file/receipt interruptions, offline-bypass refusal, interrupted abort direction,
lost StopUnit/StartUnit replies, failed readiness, maintenance refusal of desktop
Start/SetConfig, and active sharing refusal before service changes.

Further checks cover runtime masks, existing jobs, unit overrides, inactive
/etc unit definitions that outrank runtime masks, foreign bus owners, pending
networking evidence, changed manager identity, incomplete predecessor snapshots,
and binaries lacking the maintenance protocol. Ready-state recovery preserves a
new desktop Start, refuses a deleted committed receipt, and can restart an exited
idle replacement. Existing desktop introspection, reply decoders, asynchronous
clients/signals, authorization, backend cleanup/recovery and suspend/resume checks
also pass.

Ignored evidence retains `artifacts/validation-f530c10.log`,
`artifacts/create-ap-stage-f530c10/` and `artifacts/nm-stage-f530c10/`.
The final log and both manifests were copied locally and hashes matched.

## Limits and retained gates

No active-host installation, service reload/restart, policy change, hotspot
activation, suspend or physical networking was performed. Read-only local checks
showed Wi-Fi connected, the original installed daemon retaining its process and
the original project checkout clean.

Real UID-0 installation and systemd integration, system-bus/Polkit policy reloads,
distribution mount layouts and security metadata, boot behavior, failure/restart
transitions, storage power loss, release signatures, package-manager coordination,
cross-version state/configuration and physical network handover remain gates.
Private fixtures do not certify privileged host deployment.

This first implementation requires idle sharing and complete, managed native
snapshots of the same backend. Active legacy daemons, unmanaged predecessors and
old binaries without the maintenance protocol require controlled offline adoption.
Unexpected jobs/transitional unit states and bus/manager replacement are refused
for administrator review; unrelated jobs are not cancelled and unknown PIDs are
not killed. The protocol marker is a compatibility declaration, not authentication
of arbitrary executable code. Use audited, trusted staged builds and keep the
matching installer and receipts available.

See [live handover](native-live-deployment.md) for commands, maintenance semantics
and recovery boundaries, and the linked official systemd D-Bus API reference.
