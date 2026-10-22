# Native offline deployment race and recovery audit

Audited and validated on 2026-10-22 using ordinary-user disposable images on
Ubuntu 26.10 (Stonking Stingray, stable), x86_64, Rust/Cargo 1.97.1,
rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Fixes

- **Interrupted abort reversed direction.** Previously, rollback of an unfinished
  upgrade restored files without recording the decision to abort. Another
  interruption followed by recovery could reinstall the rejected upgrade.
  Receipts now sync an `aborting` decision before restoring anything; recovery
  and repeated pending rollbacks finish that decision and retain older history.
- **Absent runtime lock left an exclusion gap.** Previously, deployment held the
  native backend lock only if its file already existed, and scanned networking
  evidence only before acquisition. Mutating commands now create/reserve that
  lock, retain it through receipt commit and rescan evidence after acquisition.
  Read-only plans/status do not create a runtime directory or lock.
- **Matching files did not establish rename durability.** Recovery previously
  skipped files already matching the destination, including a rename whose
  publisher could have died before directory fsync. Every destination file and
  its parent are now synced before committing, including matching files and
  the parent of a deleted slot.
- **Unusable rollback changed a stable receipt.** A completed installation's
  rollback previously published a pending target before checking its saved
  blobs and configuration. Those checks now precede journal publication.

Version-1 receipts lacking `aborting` retain forward recovery. New receipts
are rejected by older strict-schema installers, avoiding an unsafe downgrade
of recovery semantics. The matching installer must remain available for recovery.
Configuration bytes, payload paths, desktop files and public D-Bus interfaces
are unchanged. No dependencies were added.

## Exact candidate

- Implementation: `23cd0641c64426b79ab54de4a1ef2f35edcfeaa1`
- Source tree: `63d4cd5a7e08f026d204f852ce2b5b255763cba0`
- Source patch SHA-256 against `324661f`: `f252ec231613094901bfe460ba3ef9ef3a2095d69d8dc5abee6e5e68fb78dd58`
- Cargo.lock SHA-256: `a3db3caf038220ff20b1c23ef3ee25742ff56bd983062e89df91e39f18ba20e4`
- Optimized service/helper SHA-256: `9c4f098c787b2d87761448567511aeff241c0fe739b51027b31e66bb42c0bd47`
- create_ap manifest SHA-256: `e338efb53acfd323b4c361af4cd7942b550f34b7341de679211be0d19cb9b163`
- NetworkManager manifest SHA-256: `56c873b98b94fed95314b53149d1eb46bb84fb4121f283d610bb9429f8748b5b`
- Validation log SHA-256: `bc4d7ac8b813278339adf884675a3384da37499ebaef1a8b2b0449584ba2ef79`

The remote source patch matched before aligning its checkout with the exact
candidate. The checkout was clean at this revision before and after all checks.
The following documentation commit records results only.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 46 passed |
| Full Python suite against optimized executable/examples | 223 passed, 249.188 seconds |
| Clippy all-targets with warnings denied; rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |

All 269 test methods passed. Existing desktop introspection, reply decoders,
asynchronous client/signals, authorization, backend supervision, cleanup,
recovery and suspend/resume checks passed. Cargo.lock and dependencies are unchanged.

Six additional deployment methods cover abort interruptions at journal/file/
commit phases, repeated recovery and pending rollback, first-install abort,
preservation of older rollback history, rollback preflight failures, an initially
missing runtime lock, competing backend/deployment lock acquisition, networking
evidence arriving after the first scan, legacy receipts and invalid abort states.
The 20 deployment methods run the actual optimized executable with real file
operations and locks. Fixture pause/continuation controls are bounded and only
available through the explicit ordinary-user fixture entry point.

The repeated first-install abort regression was also run against the retained
pre-audit optimized installer (SHA-256
`52fa8694cb2b0372eb811c1c096b57a2305f9583d9f99795945e4b990cf50bbb`).
It reproduced the old recovery-direction bug with the expected assertion failure;
the candidate passes that same regression. Its separate negative-control log is
retained as `artifacts/deployment-audit-old-regression.log`.

Ignored evidence retains `artifacts/validation-23cd064.log`, both staging
directories and the negative-control log on the build host. The final log,
negative-control log and both manifests were also copied locally and their
hashes matched. The negative-control log SHA-256 is
`86e23ec208fcc9851cceb2d316cd281dca19418e71b73480ee70a166c10bcb33`.

## Limits

Crash fixtures interrupt at logical publication points; they do not simulate
storage power loss or prove filesystem/controller durability. The additional
fsync of matching destinations closes a code-inspection gap; power-cut testing
remains a release gate. Arbitrary administrator writes and legacy processes
that do not honor the native lock still require an offline, quiescent image.

Only disposable image files were modified. No active-host installation, service
reload/restart, policy change, hotspot activation, suspend or physical networking
was performed. Read-only local checks before and after validation showed Wi-Fi
connected, the original installed daemon retaining its process, and the original
project checkout clean. Real UID-0 installation, other group IDs, bind/multi-mount layouts,
extended metadata, signatures/packages, live service handover, boot readiness and
cross-version configuration/state migrations remain controlled deployment gates.
See [native deployment](native-deployment.md) for the operational boundary.
