# Release qualification ledger

Status on 2026-11-12: stable release qualification is incomplete. Candidate
preparation and hardware qualification are separate steps. Do not replace the
published 0.1.2 asset with a differently built package under the same version.

## Evidence

- [8d0a5bb qualification](qualification-8d0a5bb-20261112.md) passed 39
  installed-system checks using corrected harness `4ecc12a`. Hosted CI passed
  80 Rust tests, 459 Python tests and 49 check groups without reported skips.
  It includes the AppArmor startup fix. The subsequent
  [physical qualification attempt](qualification-physical-20261112.md) verified
  candidate NetworkManager AP+STA traffic and safe active-upgrade cleanup.
  The [corrected follow-up](qualification-recovery-20261112.md) passed
  NetworkManager automatic resume, Stop suppression, recovery and controlled
  two-client LAN traffic. External timeouts and remaining matrix gates are open.

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
  8d0a5bb. Physical coverage is limited to the results recorded below.

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
| Default NetworkManager backend | AP+STA, lifecycle recovery and controlled two-client traffic passed on one iwlwifi radio (Android API 29/34). Dedicated-adapter and broader matrix coverage remain required. |
| Active-sharing upgrade | Published 0.1.2 → candidate 0.1.3~rc2 passed safe cleanup with active Android traffic; sharing was interrupted and remained idle. Active downgrade and other backend coverage remain required. |
| Automatic resume | NetworkManager passed 57-second physical sleep, automatic sharing restart, tablet traffic after reselection, and Stop suppression. Repeat on native create_ap; automatic client network reselection is not established. |
| Recovery | NetworkManager passed cancellation, crash cleanup/restart, upstream loss and radio recovery with preserved profiles; sharing after loss required explicit Start. Native create_ap physical recovery remains required. |
| Hardware/client matrix | Two concurrent API 29/34 clients passed a 604-second controlled LAN soak. The 610-second Internet soak had two connect timeouts, also seen in direct external controls; individual attribution remains open. Additional adapters and bands remain unqualified. |
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
