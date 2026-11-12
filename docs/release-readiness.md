# Release qualification ledger

Status on 2026-11-12: stable release qualification is incomplete. Candidate
preparation and hardware qualification are separate steps. Do not replace the
published 0.1.2 asset with a differently built package under the same version.

## Evidence

- [8d0a5bb qualification](qualification-8d0a5bb-20261112.md) passed 39
  installed-system checks using corrected harness `4ecc12a`. Hosted CI passed
  80 Rust tests, 459 Python tests and 49 check groups without reported skips.
  It includes the AppArmor startup fix. No physical rerun was performed.

- Main `683668b9c3cf1be717bf9487dd2b0e97462d51d5` passed the
  [hosted offline workflow](https://github.com/3togo/relayforge/actions/runs/42776376770)
  on 2026-11-10, including applet checks and isolated package file lifecycle.
- [9526f6d qualification](qualification-9526f6d-20261107.md) records 39 real
  Ubuntu 26.10 x86-64 installed-system checks, and native create_ap traffic on a
  dedicated 2.4 GHz adapter with Ethernet upstream and one Android client.
  DHCP, gateway ping, DNS, certificate-verified HTTPS, suspend, manual AP restart
  and cleanup passed. This is evidence for that source, not a later candidate.
- Later main changes include process-exit cleanup and GNOME switch feedback
  suppression. Their offline checks passed; installed-system reruns subsequently passed on
  8d0a5bb. Physical reruns against final packages remain required.

## Prepare exact candidate artifacts

Commit intended changes, then run as an ordinary user on the target distribution:

```sh
python3 tools/prepare-release.py --revision HEAD \
  --version 0.1.3~rc1 --output /absolute/new/candidate-directory
```

Scratch ancestors must have trusted ownership and no group/world write access,
as required by deployment fixtures. The command checks this before building,
and runs subprocesses with umask 0022 so Debian control directories have valid
permissions even when the invoking shell uses umask 0077.

The tool exports committed source into a private directory under `~/git/temp/`,
keeps child-process temporary files there and binds Cargo output to that
snapshot. It runs the full offline suite including both applet modes, rejects failures and
reported unittest skips, and packages the freshly validated ELF for both
backends with tray and GNOME companions. Workspace edits are excluded.
`candidate.json` records the source/tree/archive identity, executable digest and
all output digests. It is emitted only after successful checks and packaging.
A failed run preserves its output/logs and never produces a success receipt.
The packages are candidates; this command does not publish or install them.

## Qualification still required

| Gate | Required evidence |
| --- | --- |
| Final installed-system run | Passed on 8d0a5bb with harness 4ecc12a: 39 checks and matched package hashes. Repeat if implementation or packaging changes. |
| Final create_ap traffic | Repeat DHCP, DNS, verified HTTPS, stop and cleanup on dedicated hardware using final packages. |
| Default NetworkManager backend | Dedicated adapter and AP+STA where supported; upstream must remain connected, with actual client traffic and clean teardown. |
| Active-sharing upgrade | Upgrade and downgrade while a client transfers traffic; record interruption, service restart behavior, final ownership and cleanup. Never infer continuity from idle upgrade checks. |
| Automatic resume | Verify a retained authorized request restarts automatically, client rejoins, traffic recovers and Stop prevents later restart; manual restart does not close this gate. |
| Recovery | Radio off/on, upstream loss/recovery, cancellation and service crash/restart; verify no foreign resources are removed and owned state is recovered or clearly retained on ambiguity. |
| Hardware/client matrix | Two concurrent clients, supported bands, multiple adapters/client OSes and a measured soak; record concrete versions and limitations. |
| Release review | Align README, changelog, feature matrix and package metadata with evidence; use a new version, verify candidate digests and publish matching source/receipts. |

Bridge and band-switch opt-ins remain experimental and absent from packaged
units. Other distributions and architectures remain unqualified until tested.
AppArmor is in complain mode; enforcement is not certified. Native create_ap
refuses active firewalld for new local rules; do not describe it as qualified
firewalld passthrough integration. Interactive Polkit-agent prompts need their
own desktop validation.

Use the maintained [VM procedure](../tools/system-validation/README.md) for
installed-system tests. Physical tests require an explicitly selected bench,
dedicated radio/client resources, saved original state and verified restoration.
Do not run them on the development connection or substitute simulated devices
for physical qualification. Keep raw operational evidence outside source control.
