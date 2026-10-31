# First feature-parity implementation batch — 2026-10-23

This is historical validation of the candidate identified below. The final merged
package includes incoming maintainer hooks and requires separate OS qualification.


This batch implements desktop feedback, optional controls, migration, integration
source retention/build tooling, packaging metadata, source export and repeatable
offline validation. It does **not** establish complete backend or hardware parity.
Open work and acceptance criteria remain in `feature-parity-plan.md`.

## Source provenance

- RelayForge base: `57c9980084570668577228c324c7a8ea1842f73e`.
- Wi-Fi Relay reference and unchanged vendor baseline:
  `12f9f78e2cf4e9f41abacf94fbf5f5524bf47814`.
- Final code candidate source-receipt SHA-256:
  `36cf24e820de1a62f0b198a289ba8740e02881f4f22d61d381f1bb2ff00a5eff`.
- Offline runner build-source identity:
  `f81caca43310684c82ec77e752bf9922c127e814803681adc09489915f8bb8b7`.

The receipt records each source file's SHA-256. This is an uncommitted candidate,
not a new commit or release. The source-only snapshot explicitly substituted
base-revision bytes for the two unrelated dirty deployment implementation files;
their local changes were preserved and were not part of validation. Subsequent
edits to this report, the parity matrix/plan, packaging-scope document and
attribution prose do not change tested implementation bytes.

Earlier checkpoints are retained as diagnostics: `b197a6fd7ae9` passed the earlier
full suite; `1dfbe088b20e` and `b789c7a7cbe3` passed focused cache/packaging checks;
`2e219691df58` passed the initial tray split and lifecycle checks. Final candidate
evidence takes precedence over those checkpoints.

## Verified environment

Ubuntu 26.10 (Stonking Stingray), x86_64; Rust/Cargo 1.97.1; GCC 15.3.0;
Clang 22.1.8; Python 3.14.8; dpkg-deb 1.23.7; GTK3 3.24.52,
GTK4 4.24.1 and libadwaita 1.10.0. Tests used a separate source directory,
source-bound Cargo target, private D-Bus sessions and hidden desktop test models.
No live hotspot service or installed networking package was replaced.

The applet harness used an isolated extraction of `libjansson-dev` 2.15.1-1,
not a host package installation. Download SHA-256:
`7b2de6adaecc980180be167990ab41ee9254bc8d88cf935d9317e0de7dc5c706`.

## Offline checks

Final full-suite result is recorded in the retained `offline-hcn26c35` logs and
`validation.json`: runner exit status 0, `full_python_suite: true`, no skipped cases.

| Check | Result |
| --- | --- |
| Fresh locked offline release build, all workspace targets | Passed |
| Rust unit tests | 51 passed (18 core, 33 service) |
| Top-level Python suite | 264 passed across 25 modules |
| Desktop regression suite | 63 passed; Node controller harness also passed |
| Applet harness | 17 optimized and 17 ASan/UBSan cases passed |
| Provenance | 48 vendor and 10 integration files verified |
| Package lifecycle and metadata followup | All recorded steps passed |

The runner builds all workspace targets in release mode with locked offline
dependencies, then runs each Python module separately with bounded timeouts.
GTK4/editor tests and GTK3 tray tests run in separate processes. The applet runs
optimized and with AddressSanitizer/UndefinedBehaviorSanitizer on a private bus.
Vendor and integration manifests are verified independently.

Migration tests cover exact-template rotation, customized-password preservation,
dry-run/idempotency, literal credentials/comments/line endings and unsafe-file
refusals. Actual CLI cases use private disposable files. Migration never activates
sharing and refuses writes to the live configuration path.

Source export tests use disposable Git repositories: committed-tree identity,
deterministic archive bytes, omission of dirty/untracked work and Git history,
existing-output protection, invalid revisions and tracked-build-product refusal.

Two diagnostic problems were resolved before the final run: preserved archive
timestamps could reuse stale Cargo artifacts, so targets are now bound to source
hashes; oversized debug executables exceeded deployment fixture limits, so the
runner uses release builds. Earlier stale-artifact failures/aborted runs are not
counted as passing evidence. The deployment executable-size guard was preserved.

## Package and metadata checks

Both native backend variants passed package assembly regression tests. The base
package has no tray-only GTK3/AppIndicator dependency or tray file ownership;
the recommended exact-version tray companion owns its launcher and autostart
conffile. Optional GNOME controls retain the supported Shell range and explicit
startup preferences. Packages contain no automatic activation/restart hooks.

Isolated file lifecycle passed both combined-package → split-controls and
split-controls → newer-split-controls transitions, including GNOME login opt-in.
Checks cover install, upgrade, independent control removal/reinstallation,
remove/purge, custom autostart preservation, private administrator configuration
and per-user preference preservation. They use a private dpkg root with forced
dependencies; lifecycle payloads use a small stand-in ELF and are not deliverables.
They do not certify apt resolution, runtime dependencies or real service lifecycle.

The disposable container was Ubuntu 26.10, image digest
`sha256:93e29beff5257b2cdf1c024d831f6d2997e53655a653ff76a811fc9e6564a719`,
with networking disabled, a read-only root/source mount, a temporary dpkg root,
no network-administration capability and only the file-access capability needed
for private fixture files. No packages were installed on the development host.

`appstreamcli validate --no-net` and both `groff -Tutf8 -man` renders passed.
Pedantic AppStream validation reported only the retained uppercase compatibility
ID and absent release history. Neither inventing a release nor changing the
existing desktop ID is part of this batch. Actual graphical catalog/window/Shell
presentation remains unverified.

The freshly built service ELF is 10,431,392 bytes, SHA-256
`c88ca14f199956354195de3c0acf4b61013459a12dce8df0da46458153f9b14d`.
It was assembled into uninstalled `0.1.0~parity1` candidate packages:

| Package | Bytes | SHA-256 |
| --- | ---: | --- |
| relayforge, amd64 | 2,522,580 | `5f4f42e9b558fc6d1fc3070c7557d2c1701167a6e3ba1b20426d96693ee7c594` |
| relayforge-tray, all | 5,902 | `95b9cc9c7ddd138b459ec479f42e87555128938a331767d05711b6c5615cd45b` |
| relayforge-gnome, all | 7,324 | `90bff5ae93edf5bdd6c49dc9dbcaebf12e2a148026c4c78cd9bf825af638fa04` |

Package hashes identify these specific assembly outputs; Debian package archive
reproducibility and dependency installation were not certified. No package was
published, installed on a live host or used to activate sharing.

## Limits and remaining work

NetworkManager device-state snapshots before/after completed offline and package
runs were unchanged. This is corroboration of the offline scope, not a traffic or
hardware recovery test. No spare inactive radio was available for a safe bench.

Native band-switch transactions, bridge ownership/recovery and additional legacy
driver implementations are still open. Global regulatory/MAC/module/firewall
policies remain explicit design work. Full downstream distribution builds, real
systemd/D-Bus/Polkit/Shell integration, DHCP/DNS/HTTPS/client traffic and physical
sleep/crash/recovery checks remain open. RPM has no selected target. The added
hosted CI workflow has not yet run; local/offline results do not certify it.

Raw receipts/logs are retained outside repository history under the ignored
`artifacts/feature-parity-validation-20261023/` tree. They may include operational
paths and are not public release evidence. The original repository, pinned
vendor source and unrelated deployment work remain unchanged.
