# Native offline deployment validation

Validated on 2026-10-22 as an ordinary user in a separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, stable), x86_64, Rust/Cargo 1.97.1,
rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `e01dd3696c1f71a5ee827eda0b1fd59a482327d7`
- Source tree: `75d1b6a19f63caf276e02657c4d64d90d823aa30`
- Cargo.lock SHA-256: `a3db3caf038220ff20b1c23ef3ee25742ff56bd983062e89df91e39f18ba20e4`
- Optimized service and native helper SHA-256: `52fa8694cb2b0372eb811c1c096b57a2305f9583d9f99795945e4b990cf50bbb`
- create_ap staging manifest SHA-256: `db1a9b9afe7a0736f332cd26f177ce5230b0261045e3175c8aae8c312596214f`
- NetworkManager staging manifest SHA-256: `52cdfd2af896efdc2577638fbcfdb1df46e917a626e47fcdeb11acd5d0806f74`
- Validation log SHA-256: `db4bd360f213758567c7485edc1d6396bd1eb1b0e5a1fbd1636b0ea98a8983b6`

The remote checkout matched this exact committed candidate with no tracked
changes or extra untracked source before final validation and remained clean
afterward. The subsequent documentation commit records results only. Dependencies
and Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 46 passed |
| Full Python suite against optimized service and examples | 217 passed |
| Clippy with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |

All 263 test methods passed. Fourteen new deployment methods execute the actual
Rust CLI against disposable ordinary-user image directories and real staged
payloads. They exercise real descriptor-relative file operations, hashing,
atomic renames, file/directory fsync, ownership/mode restoration and flock.
Versioned byte fixtures provide different payload hashes without executing the
installed executable. They establish file transaction behavior, not service
compatibility between independently built historical releases.

Coverage includes both backend payloads; read-only plans; idempotent install and
recovery; upgrades and pre-installation baseline restoration; interruption after
journal publication, every file slot, all files and final receipt commit;
interrupted rollback completion and upgrade abort without losing rollback history;
changed installed files, corrupt receipts and blobs, unsafe manifest paths,
symlinks, hardlinks and FIFOs; foreign-image receipt rejection; deployment/runtime
locks; pending networking records; production privilege and live-root refusal;
explicit private configuration and backend checks; preservation of new user edits;
and configuration/desktop sentinels remaining unchanged.

An additional staging regression checks permissive and restrictive caller umasks.
Staged directories and manifest now receive explicit permissions. A deployment
regression also checks installation directory permissions under a restrictive
umask. Existing desktop introspection, decoders, asynchronous clients/signals,
authorization, backend supervision, cleanup, recovery and suspend/resume tests pass.

## Evidence and limits

Ignored evidence retains `artifacts/validation-e01dd36.log`,
`artifacts/create-ap-stage-e01dd36/` and `artifacts/nm-stage-e01dd36/`.
The final log and both manifests are retained locally as well.

Only disposable image files were installed/upgraded/restored. No packages were
installed and no active-host system service was installed, enabled, loaded or
restarted. No bus policy was reloaded, hotspot activated, or machine suspended.
Read-only local audits showed Wi-Fi connected, the original installed daemon
retaining its process, and the original project checkout clean.

See [native deployment](native-deployment.md) for commands, recovery direction and
ownership boundaries. The engine refuses the live root and never contacts a
service manager or D-Bus. Configuration remains outside payload snapshots.
Receipts remain bound to their original image directory identity; administrative
writers must stay quiescent, and old blobs/crash-left temporary files are retained
for review rather than garbage-collected.

Actual UID-0 image installation, restoration of other group IDs, bind/multi-mount
layouts, security labels/ACLs/capabilities, publisher signatures, distribution
packages, live service handover, desktop activation/authorization, boot readiness,
physical networking and cross-version configuration/state migrations remain
controlled deployment gates. No privileged production installation is certified
by ordinary-user fixture results.
