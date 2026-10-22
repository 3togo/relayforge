# Native suspend/resume audit validation

Validated on 2026-10-22 as an ordinary user in a separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, stable), x86_64, Rust/Cargo 1.97.1,
rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `b3bc71dfae5a824a9dc88387c7759258219cdae4`
- Source tree: `7a190068fbaa7a32ec744d43779e5d69a9956496`
- Cargo.lock SHA-256: `a3db3caf038220ff20b1c23ef3ee25742ff56bd983062e89df91e39f18ba20e4`
- Optimized service and native helper SHA-256: `6605153e92948796ccc96d5981ec10eed0b0c46b835a04f03cec21a683e43182`
- create_ap staging manifest SHA-256: `d269af905ec7ecbd7a69b142577c1c609e02f83d54bc9ae0c5c7fccf8228c07a`
- NetworkManager staging manifest SHA-256: `5a65802706bba70023c8874900efcd892aa3afb382fed1382a35bffbb3b84380`
- Validation log SHA-256: `458c854701d7d289191fc6227d27abaa759857d1dfc967b83d2a46cd22968b54`

The remote checkout matched this exact committed candidate with no tracked
differences or extra untracked source before validation and remained clean
afterward. The subsequent documentation commit records results only. Dependencies
and Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 46 passed |
| Full Python suite against optimized service and examples | 202 passed |
| Clippy with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |

All 248 test methods passed. Two new Rust methods verify that stale inactive
observations cannot revoke a newer intent or replace clients, and that inhibitor
completion belongs to a sleep generation rather than the user's Stop revision.
The existing stability test now models a completed inhibitor handshake.

Nine new private-bus regressions verify:

- Stop cancels Start waiting for authorization or the operation queue.
- A held post-start probe is cancelled promptly and returns no stale success.
- Initial logind setup completes before Start is exposed.
- A delayed wake inhibitor gates Start; Stop during reacquisition still permits a
  later fresh explicit Start when that descriptor arrives.
- Failed reacquisition cancels intent and subsequent monitoring can reconnect.
- Logind loss during known sleep retains the gate until a trusted awake snapshot.
- A second sleep cancels automatic activation while retaining only the original intent.
- An external configuration edit during the final stable probe prevents activation.

Existing desktop introspection, JSON decoders, asynchronous clients/signals,
authorization, native create_ap/NetworkManager lifecycle, cleanup retries and
crash/ownership recovery all pass. The held replies and fake networking operations
run on an isolated bus with disposable ordinary-user processes.

## Evidence and limits

Ignored evidence retains `artifacts/validation-b3bc71d.log`,
`artifacts/create-ap-stage-b3bc71d/` and `artifacts/nm-stage-b3bc71d/`.
The final log and both manifests are retained locally as well.

No packages were installed and no system service was installed, enabled, loaded
or restarted. No physical hotspot was activated and neither machine was
suspended. Read-only local checks showed Wi-Fi connected, the original installed
daemon retaining its process, and the original project checkout clean.

The [audit](native-suspend-audit.md) describes the fixes and retained boundaries.
External administrative changes can still race the final observation; existing
frozen-plan checks and monitoring apply. Actual logind delay deadlines, physical
sleep timing, driver behavior, DHCP/DNS/NAT, distribution policy and deployment
upgrade/rollback remain controlled hardware/deployment gates. Simulation does
not certify those properties.
