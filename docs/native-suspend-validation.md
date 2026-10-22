# Native suspend/resume validation

Validated on 2026-10-22 as an ordinary user in a separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, stable), x86_64, Rust/Cargo 1.97.1,
rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `8055c926f7b65dcac701222d113e20e984669eda`
- Source tree: `c175fcc548400cc72fad4cc8f0612e1b93267251`
- Cargo.lock SHA-256: `a3db3caf038220ff20b1c23ef3ee25742ff56bd983062e89df91e39f18ba20e4`
- Optimized service and native helper SHA-256: `38e795a8ceba464d1bf150d4419dd49826298561a6f48263256ae2f00b9a1786`
- create_ap staging manifest SHA-256: `bca58b8af7fa2e37622f85360d03cb2c2a178a2c382845676403276e9992c3af`
- NetworkManager staging manifest SHA-256: `e0bea57af974b3ac948c7c0661cf40042d18467382e7b40945ab3ee66aff2e7b`

The remote checkout matched the committed candidate with no tracked differences
or extra untracked source before validation and remained clean afterward. The
subsequent documentation commit records results only. The service now explicitly
depends on the already-locked futures-util 0.3.34; resolved crate versions are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 44 passed |
| Full Python suite against optimized service and examples | 193 passed |
| Clippy with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |

All 237 test methods passed. Two new Rust policy tests cover sleep/cleanup gates,
monotonic stability, channel changes and stale authorization revisions. Twelve
new Python methods use a private bus, simulated NetworkManager/kernel and a fake
logind that passes real Unix inhibitor descriptors. They cover:

- Sleep cleanup and inhibitor release, then reacquisition after wake.
- Native create_ap supervisor/runtime cleanup and restart with disposable children.
- Original-profile roaming with a fresh channel and stable association.
- Stop, changed settings, different profiles, absent association and replaced adapters.
- Cleanup failure, retained evidence, blocked relaunch and explicit Stop retry.
- Sleep during explicit startup and Stop during automatic activation.
- Failed automatic activation, confirmed cleanup and delayed retry.
- Logind loss, malformed signals and forged signals from another connection.
- No previous Start, wake-to-idle behavior and read-only status requests.

Existing desktop introspection, reply decoding, asynchronous clients/signals,
authorization, configuration, cancellation and crash/ownership recovery tests
also pass. Production service and desktop controls were not installed or changed.

## Evidence and limits

Ignored evidence includes `artifacts/validation-8055c92.log`,
`artifacts/staging-8055c92.log`, `artifacts/create-ap-stage-8055c92/` and
`artifacts/nm-stage-8055c92/`. Both logs and manifests are retained locally.
The validation log retains an initial staging CLI error: the invocation supplied
an unsupported output option after all tests had passed. Corrected `--output`
invocations completed both stages on the same unchanged candidate; their results
and artifact hashes are in the separate staging log.

No packages were installed and no system service was installed, loaded, enabled
or restarted. Neither machine was suspended and no physical hotspot was
activated. Read-only local audits showed Wi-Fi connected and the original
installed daemon retaining its process. The original project checkout stayed clean.

See [native suspend behavior](native-suspend.md) for the authorization policy and
failure boundaries. Intent is memory-only, limited to sleep recovery of a prior
explicit Start and never reconnects upstream Wi-Fi. Missing trustworthy logind
or connected-profile evidence leaves manual sharing available without granting
automatic recovery. Dedicated adapters without such an upstream require manual
Start after sleep. Unexpected backend failure outside sleep also remains an
explicit-Start boundary.

Real logind delay deadlines, physical suspend/resume timing, driver behavior,
DHCP/DNS/NAT continuity, distro authorization and install/upgrade/rollback remain
controlled hardware/deployment gates. These offline results do not certify them.
