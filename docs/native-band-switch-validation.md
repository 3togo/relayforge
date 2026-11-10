# Native band-switch implementation and validation — 2026-10-23

This continues tasks 11–13 of the parity plan. It adds an experimental explicit
BSSID-only reassociation controller and completes the existing-bridge design
checkpoint. Native bridge activation is still unavailable. These are offline
implementation results, not hardware or release certification.

## Implemented behavior

- An administrator must append `--allow-band-switch` to a native service command.
  Packaged units omit it; disabled requests do not scan or reconnect.
- The existing authorized `SwitchBandAndReconnect` method operates only while
  sharing is stopped and sleep/cleanup gates permit it. Ordinary Start continues
  to preserve upstream connectivity.
- A bounded confirmed scan selects the strongest fresh same-SSID candidate on an
  AP-permitted frequency. Saved band/BSSID/channel restrictions are refused.
- The controller uses the existing profile and a specific AP. It does not request
  secrets, edit/save profiles, change autoconnect or create automatic checkpoints.
  NetworkManager and radio observations must agree before success is reported.
- A private write-ahead journal binds recovery to bus GUID/owner, device identity,
  profile UUID/settings fingerprint and the returned activation identity. It
  contains neither plaintext SSID nor credentials. Unknown replies, foreign
  changes and failed restoration retain evidence and block further startup.
- Stop/sleep cancel in-flight switching. Confirmed owned activations can restore
  the original AP; an already-returned restoration is verified after a crash
  without issuing another activation. Removing the opt-in does not bypass recovery.

The four journal phases and provider race limits are documented in
`native-feature-policy.md`. `native-bridge-design.md` records the next transaction
contract, including hostapd's own bridge-attachment boundary; task 14 remains open.

## Exact source and environment

Base commit: `57c9980084570668577228c324c7a8ea1842f73e` with the previously validated
uncommitted parity work. Both unrelated dirty deployment implementation files were
replaced with base-revision bytes only in the validation snapshot; the local
deployment diff was preserved byte-for-byte.

Full-suite source-receipt SHA-256:
`b8bf7bcad72e43a3660c29defdf3855a6e12b820b29095d0db94a8df94930976`.
Source-bound Cargo build identity:
`5ec81326eb02a135dd59e78d3c7d6ad0b5b7f9cd06342a856c840994f876f66b`.

Packaging/metadata followup source-receipt SHA-256:
`db4b16238b71b02a59aea26bbfe121baa3f3fa5b589aa3dfa6048c1477d4ebe9`.
All Rust controller/service/power code and band-switch fixture/test bytes match
the full-suite snapshot. Followup differences are documentation, package metadata,
the policy-document copy and focused package assertions. This report and the final
plan-ledger update were written after testing; no implementation changed afterward.

Verified remote environment: Ubuntu 26.10 (Stonking Stingray), x86_64;
Rust/Cargo 1.97.1, GCC 15.3.0, Clang 22.1.8, Python 3.14.8,
dpkg-deb 1.23.7, GTK3 3.24.52, GTK4 4.24.1, libadwaita 1.10.0.
Builds used a separate exact source snapshot and source-bound release target.

## Results

Full offline runner `offline-90k38_9t`: exit status 0, `full_python_suite: true`.

| Check | Result |
| --- | --- |
| Locked offline release build, all workspace targets | Passed |
| Rust tests | 51 passed |
| Python suite | 281 passed across 26 modules, including 17 new band-switch cases |
| Desktop suite | 63 passed; Node controller harness passed |
| Applet harness | 17 optimized and 17 ASan/UBSan cases passed |
| Provenance | 48 vendor and 10 integration files verified |
| Packaging followup | 3 package cases passed for both backends, including absent opt-in flags and installed policy documentation |
| AppStream/manpage followup | Both passed |

The new cases cover default refusal/no-op, candidate strength/freshness/SSID,
profile pinning, malformed BSSID, incorrect association and restoration, Stop and
sleep, cancellation during final verification, external profile/device/AP/active
changes, provider replacement, ambiguous/reused replies, corrupted journals,
crashes during switching/restoration and refusal while a hotspot is active.
No test cases were skipped.

An earlier candidate had a compile-time GUID conversion error; another diagnostic
run exposed a 2.4-GHz-only assertion in the fake provider's AP setup. Both were
corrected before the successful final run. Those diagnostic runs are not counted
as passing evidence. The final checks also include the tightened same-activation
AP-change and reused-activation ownership guards.

## Limits and preservation

All reassociation, failure and recovery operations targeted a simulated provider
on private D-Bus sessions and disposable files. They never contacted the installed
NetworkManager service or ran kernel Wi-Fi mutation commands. The full runner's
before/after NetworkManager device-state snapshots were identical; the local
active Wi-Fi also remained connected. No networking service or installed tray was
restarted, and no candidate package was installed or published.

Real association, traffic, provider races and driver behavior still require a
dedicated radio bench. NetworkManager has no atomic activate-if-current guard;
the opt-in requires a controlled session. The pinned-profile editing fallback is
not implemented. Ambiguous request phases deliberately require inspection rather
than blind restoration or journal deletion. These limits prevent claiming full
band-switch parity or hardware readiness.

The ignored `artifacts/native-band-switch-validation-20261023/` tree retains raw
logs, manifests and receipts. Machine paths and operational records stay outside
repository history. The pinned vendor/original sources and unrelated deployment
edits were preserved. No new commit or push was performed.
