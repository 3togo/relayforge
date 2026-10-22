# Rust create_ap supervision validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8
and systemd 261.3. Builds, automated tests and staging ran as an ordinary user in
the separate remote development checkout. No native service was installed or
activated. The laptop's existing Wi-Fi and installed daemon were left running.

## Exact candidate

- Implementation: `ff9e9058489587bb8e0696510bdc0df53a06a903`
- Source tree: `869b1f28176cada4eb41a08f4414996986be78e0`
- Cargo.lock SHA-256:
  `84b72fa4915a5f4bed40bc217cc6e4891592f7f27d0bacacb557770d6a86d81c`
- Optimized Linux x86_64 executable SHA-256: `2ffb0a9508f4d34febd41b7a4e537637605e7010564624f02fdec8be4853b0cd`
- create_ap payload manifest SHA-256: `066156499ebc39f81000cc4d7006d97f2ec8ce8e6120086e005a44144e4ed665`
- NetworkManager payload manifest SHA-256: `1b29cf57551b8c0641e1e09f1f4c77862160cc7d009da156175e413d544531d0`

The complete source, including new files and the follow-up cleanup/lock fixes,
was synchronized to the build checkout. Git verified identical tracked content,
no extra untracked source and a clean checkout at this revision. A subsequent
documentation commit only records results and links this report.

## Results

| Check | Result |
| --- | --- |
| Optimized locked offline workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 35 passed; three consecutive runs |
| Python suite against optimized service and parity executables | 64 passed |
| rustfmt | Passed |
| Clippy, workspace/all targets, warnings denied | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap staging | Eight payload files plus hash/mode manifest |
| NetworkManager staging | Six payload files plus hash/mode manifest |
| Both alternative systemd units, static verification | Passed separately with the built executable |

There are 99 distinct test methods. The 13 create_ap integration methods include
multiple subcases: valid readiness, idempotent Start/Stop, client reports, private
configuration and credential-free ownership/argv, foreign snapshot preservation,
failure before PID creation, partial resources, incorrect readiness PID, output
flood, wrong AP channel, startup deadline, forced termination, cancellation,
responsive status, failed cleanup and retry, service death followed by recovery
of the live orphan, helper death, upstream/channel drift, execution-gate EOF,
SIGTERM cleanup, changed/missing snapshot ownership, missing snapshot and PID,
backend restrictions and the unmodified asynchronous desktop client/signals.
Band-switch, bridge, regulatory and connected NO_VIRT requests launch no helper.

Rust tests include refusing signals for changed argv or reused process birth,
private-file bounds and link safety, journal exclusion across native modes and
explicit lease release across concurrent forks. The Rust suite was repeated to
check the observed fork/lock lifetime race after its fix. Existing NetworkManager,
configuration, Unicode parity, authorization, radio and compatibility-worker
checks continue to pass.

The Bash simulator and replayed radio data use temporary directories and a
private D-Bus session; no test runs the real create_ap networking path. Static
unit verification used one alternative unit at a time, with only its executable
path changed to the build artifact. It returned zero; an unrelated installed
unit emitted a legacy PIDFile warning. No unit was loaded or started.

## Evidence and remaining boundaries

The build checkout retains `artifacts/validation-ff9e905.log`,
`artifacts/create-ap-stage-ff9e905/` and `artifacts/nm-stage-ff9e905/`. The final
log and staged manifests are also retained locally under ignored artifacts.
Staging contains no credentials, machine configuration or desktop replacements.

Rust now owns process supervision, readiness, cancellation, monitoring and
selection of owned recovery work. The unmodified Bash networking engine and its
internal Python snapshot/recovery helper remain dependencies. Native create_ap
is opt-in with BACKEND=create_ap; band switching, bridge mode, COUNTRY overrides,
automatic resume and the Python supervisor's additional firewalld workaround
remain outside this path. See [the operational guide](native-create-ap.md).

These results do not establish physical AP activation, real driver behavior,
DHCP/DNS/NAT connectivity, Polkit-agent interaction, suspend/resume, installation
or rollback. The original source checkout was unchanged. Read-only checks still
showed the laptop connected and its installed daemon running with its original
process identity. No networking/service configuration was changed.
