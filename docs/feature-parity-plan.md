# Wi-Fi Relay feature parity work plan

Baseline: local Wi-Fi Relay revision `12f9f78e2cf4e9f41abacf94fbf5f5524bf47814`
and RelayForge `57c9980084570668577228c324c7a8ea1842f73e`.
The original repository and pinned vendor snapshot remain unchanged. Existing
uncommitted deployment changes are outside this work.

## Rules and completion criteria

Work through the numbered tasks in dependency order. Record implementation,
offline checks and physical validation separately; a source import or passing
mock test does not establish production parity. Keep the current Wi-Fi and
networking services untouched. Builds and automated suites use an isolated remote
source snapshot; hardware-dependent checks require a later controlled window.
Do not install downstream networking packages or enable experimental behavior on
the development hosts. Preserve settings, credentials, user startup preferences,
ownership checks, cancellation and uncertain-cleanup evidence.

Each task needs an implementation, focused regression coverage, user-facing
documentation and validation provenance. Changes must retain the ten-method,
three-signal desktop D-Bus contract. Compatibility mode remains available until
the corresponding native path has passed its acceptance checks.

## Already present

- Native configuration validation/storage, random fresh credentials, interface
  enumeration, capabilities, radio preflight and Polkit authorization.
- Native NetworkManager control and create_ap runtime, process ownership,
  firewall/network transactions, daemon configuration and crash recovery.
- Native suspend/resume intent and offline/live deployment transaction machinery;
  physical validation remains separate.
- Reused GTK settings, QR sharing, client information, startup/visibility
  preferences and the original GNOME extension source.
- Maintained tray with persistent failure reasons and channel guidance.

## Ordered implementation tasks

| ID | Task | Acceptance criteria | Status |
| --- | --- | --- | --- |
| 01 | Establish a feature inventory and evidence ledger | Every original UI/backend/packaging/integration feature has a disposition; implementation and hardware gates are distinct | Inventory recorded in feature-parity-matrix.md |
| 02 | Package optional GNOME Shell controls | Optional companion package, exact service-package dependency, supported Shell versions, preserved startup preferences, no install-time enabling or service/network changes | Implemented; both backends and optional login setup checked remotely |
| 03 | Retain optional desktop integration sources | Audited source-only imports of applet, GNOME Settings and downstream NM patches with hashes, licenses and exact target versions; no machine paths or old history | Imported; provenance verified |
| 04 | Make integration builders usable in RelayForge | Existing applet/Settings/NM builders use this project's tests, dependencies and output layout; source-version checks; build without installing or restarting anything | Build-only wrapper implemented; full distribution builds pending |
| 05 | Port desktop regression coverage | Settings edits, PSK/QR/hidden networks, clients, Show/Hide, login preferences, Quit, cancellation, owner replacement and malformed replies checked against the native contract | 67 desktop cases, controller regressions and 17-case optimized/sanitized applet harness passed |
| 06 | Explain unsupported configuration before activation | Settings/tray/GNOME controls distinguish service errors, unsupported backend options and radio restrictions; preserve the original error and give backend-appropriate remedies | Implemented for native configuration/radio blockers, empty inventory and managed desktop feedback; Shell 51 and mapped Settings/About checks passed in 18 |
| 07 | Add explicit legacy configuration migration | Rotate only an exact known factory template; preserve all customized credentials; repair absent wlan0 only with unambiguous adapter evidence; atomic writes and dry-run, no live network changes | 4 new native storage tests and 3 CLI cases passed; live writes refused |
| 08 | Complete distribution lifecycle handling | Isolated install/upgrade/remove/purge tests; conflict handling, configuration/preferences preservation, stopped/idle upgrade requirements, stale desktop cleanup and executable/service ownership | File fixtures passed; current split-package real apt/systemd lifecycle passed in 18. Former combined-package migration retains file-fixture evidence only |
| 09 | Complete optional-control lifecycle handling | Optional controls can be added/removed independently; session replacement honors disabled startup; no root GUI or automatic hotspot activation | Independent tray/GNOME companions and opt-in GNOME first-login helper implemented; bundled-to-split and split upgrade/removal fixtures passed; real-session checks remain open |
| 10 | Add repeatable CI and validation artifacts | Offline Rust/Python/GTK/GNOME checks, source/package hashes, sanitized applet tests and disposable package lifecycle; strict timeouts and explicit skips | Source-bound offline runner and pinned-action workflow added; hosted workflow run pending |
| 11 | Design explicit upstream band-switch transactions | Separate authorized action, original profile/channel identity, cancellation and durable rollback; never switch implicitly during ordinary Start | BSSID-only contract verified against primary NM API docs; journal phases and external-client race limits recorded in native-feature-policy.md |
| 12 | Implement and fault-test native band switching | Private-bus NM fixtures cover scan results, password handling, failed association, Stop/sleep races, crash recovery and restoration; unchanged desktop reply/signals | Experimental opt-in BSSID-only implementation and private-bus fault tests added; pinned-profile edits and actual-provider/hardware qualification remain open |
| 13 | Design bridge and dedicated-adapter parity | Capture original supported use cases; reject unsafe or foreign bridge/parent ownership; model address/route/NM/sysctl changes and exact restoration before implementation | Existing-bridge design complete in native-bridge-design.md; upstream-migration transaction deferred separately |
| 14 | Implement bridge ownership and recovery | Offline operation logs and failures at each boundary prove cleanup/restoration; no adoption of foreign interfaces, loss of upstream state or release of unowned leases | Existing-bridge owned-child implementation and fault/recovery tests passed behind an explicit opt-in; upstream migration and isolated traffic/hardware qualification remain open |
| 15 | Audit legacy driver/security options | Map nl80211, brcmfmac/NO_VIRT, vendor hostapd drivers and WPA options individually; explicit capabilities/refusals, no silent security downgrade; representative fixture captures | Source/policy audit complete in native-driver-security-audit.md; shared early policy and fixture profiles implemented; representative hardware captures remain open |
| 16 | Add supported driver variants | Implement only documented driver contracts backed by fixtures and ownership recovery; retain clear unsupported results for unavailable driver/toolchain combinations | Standard nl80211/dedicated-adapter software paths covered; rtl871xdrv remains unsupported pending verified hostapd/driver contracts and captures; physical qualification open |
| 17 | Resolve regulatory and optional kernel/firewall behavior | Separate country/MAC overrides, PPTP helper and firewalld behavior into explicit administrator-owned policies; no blanket firewall rewrite or implicit module loading | Read-only provisioned-country constraint and active-firewalld startup/runtime guards implemented; owned cleanup stays available. Global changes and native firewalld mutation remain unimplemented; isolated system/traffic qualification open |
| 18 | Verify actual host packaging/desktop behavior | Disposable matching-OS environment: real systemd/D-Bus/Polkit, install/upgrade/rollback/removal and GNOME Shell loading; do not use the live Wi-Fi host | Passed for Ubuntu 26.10/Shell 51 in an isolated KVM guest; real apt lifecycle, systemd/D-Bus/Polkit, mapped Settings/About, tray and extension loading. See native-system-integration-validation.md; physical traffic and other versions remain separate gates |
| 19 | Validate native hotspot traffic on a controlled bench | Dedicated AP adapter/upstream, disposable profiles/ports, DHCP/DNS/verified HTTPS, two clients, cancellation, crashes and restoration; preserve the user's existing connection | Controlled hardware window required |
| 20 | Validate recovery and compatibility matrix | Radio toggles, upstream loss, suspend/resume, restart/rejoin, bridge/band switching, multiple drivers/client OSes and soak tests with exact source/package evidence | Depends on 12, 14, 16, 17, 19 |
| 21 | Complete distribution metadata and source export | AppStream, manpages, About/version/project links and clean native source archives describe RelayForge rather than inheriting original release claims | Implemented; focused and full validation recorded in the batch report; Settings/About window mapping passed in 18 |
| 22 | Assess additional packaging formats | Record the original unvalidated RPM recipe; implement/retest only against an explicitly supported target distribution and toolchain | Assessed in packaging-support.md; RPM deferred without a selected target, no inherited support claim |
| 23 | Close parity and release gates | Update the matrix with actual evidence, residual limitations and reproducible packages; keep unsupported experimental integrations labeled; do not transfer original release certification | Depends on all applicable tasks |

## Inventory that task 01 must account for

- Desktop: GTK editor and About, Wi-Fi/security/SSID/band/channel settings,
  upstream selection, gateway and DNS, hidden SSID, MAC filtering/isolation,
  802.11 variants, QR encoding, clients, tray/GNOME status, actionable failures,
  login/visibility preferences, lifecycle/replacement and explicit Quit.
- Backend: create_ap and NM modes, AP+STA and dedicated adapters, band/channel
  constraints and optional switching, NAT/bridge/no-sharing, driver-specific
  policy, secure daemon configuration, leases/client discovery, firewall and
  forwarding ownership, cancellation, crash/unknown cleanup, sleep and resume.
- Distribution: fresh initialization, exact-template migration, interface repair,
  dependency minimization, service activation, package conflicts, optional
  controls, upgrade/remove/purge, source export and reproducible evidence.
- Optional downstream components: applet menu/visibility, control-center stable
  device tabs and Relay entry point, NM-owned AP child contract, supported source
  baselines, licenses, tests and build-only workflows.

## Validation ledger

The prior tray-only validation is recorded in `tray-feedback-validation.md`.
The first implementation batch and its exact tested candidate manifests are
recorded in `feature-parity-phase1-validation.md`. Remaining native backend and
hardware gates above are still open; passing offline checks does not close them.

The next BSSID-only native controller, its 17 new private-bus fault cases and
full-suite results are recorded in `native-band-switch-validation.md`.
Task 14's existing-bridge implementation and recovery results are recorded in
`native-bridge-validation.md`; its contract is in `native-bridge-design.md`.
The native driver/security audit and first supported software paths (tasks 15–16)
are recorded in `native-driver-security-audit.md` and
`native-driver-security-validation.md`.
Task 17's read-only country/firewall policy and recovery evidence are in
`native-regulatory-firewall-policy.md` and
`native-regulatory-firewall-validation.md`. Administrator-mutating variants
remain open. Task 18 passed for the documented Ubuntu 26.10/Shell 51 VM matrix;
the next hardware/traffic gate is task 19. Its report and exact artifact identities
are in `native-system-integration-validation.md` and the adjacent JSON record.
New bridge creation, upstream migration and physical bridge qualification remain
separate open gates.
