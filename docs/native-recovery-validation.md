# Native recovery validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8
and systemd 261.3. Builds, automated checks and staging ran as an ordinary user in
the separate remote development checkout. No system service or hotspot was
installed, enabled, stopped or started. The laptop's current Wi-Fi remained connected.

## Exact candidate

- Implementation: `f158f43cf57fc75333a6a3d8c732836d46b5d6f0`
- Source tree: `216ea5475741df300fefa953ba51027f8b8ad458`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `97338e301cedc766e23198211d019e2158a744d4fbdcd417ee3462b980217a7e`
- Native Bash adapter SHA-256: `00a22969c8760d07930b27bb7f208743fcd3a4d1efddadf331b0a3794bd37a50`
- create_ap staging manifest SHA-256: `4b7d1dd164e2eda822b5377df2004411587ff08082c2336bfac73e92d3f50a10`
- NetworkManager staging manifest SHA-256: `822c1b367f4efea0088574f5970fd4ddff6fa62868fe97d8035fbf049204d600`

Git verified the exact source revision, identical tracked files, no extra
untracked source and a clean build checkout before validation. The later
documentation commit only records results and links this report. Cargo.lock adds
sha2 0.10.9, using already pinned transitive dependencies, for compatibility with
the existing process-identity SHA-256 format.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 37 passed |
| Full Python suite against optimized service, parity and child executables | 82 passed |
| rustfmt and Clippy with warnings denied | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| Native create_ap staging | Seven files plus hash/mode manifest, no Python files |
| Native NetworkManager staging | Six files plus hash/mode manifest |
| Generated Bash syntax | Passed |
| Alternative systemd units, separate static verification | Passed |

There are 119 distinct test methods. The 17 new recovery tests exercise real
Rust CLI/process behavior with temporary runtime directories and simulated
interface/firewall data. They cover legacy identity/snapshot parity, atomic
private snapshots, literal Bash assignments and arrays, live-owner refusal,
changed/planned/reused interfaces, schema/type/path validation, symlinks,
hardlinks, FIFOs, modes and input bounds, exact child identities, TERM/KILL,
changed process birth/argv/executable, foreign configuration paths, partial
child recording, common haveged state, watchdog parent checks, cleanup verification
and retained state on failure. Load stops verified children before refusing
network cleanup when interface identity has changed.

Two lifecycle tests connect the Rust supervisor, a non-networking Bash simulator
and the Rust recovery runtime on a private bus. The unmodified asynchronous
desktop client receives status/client signals; failed cleanup retains the journal
and can be retried. Killing the supervisor leaves real disposable child processes
which the restarted service then recovers. Generated Bash snapshot-function
marshalling is also checked against the actual Rust runtime. Staging rejects
source changes even when the helper-call text is unchanged.

Rust unit checks include the privilege policy for recording dnsmasq after its
normal root privilege drop and rejection of a mismatched expected UID before
signalling. The full older configuration, authorization, protocol, radio,
NetworkManager, create_ap-supervisor and compatibility-worker suites still pass.

## Artifacts and limits

The build checkout retains `artifacts/validation-f158f43.log`,
`artifacts/create-ap-stage-f158f43/` and `artifacts/nm-stage-f158f43/`. The final log
and manifests are also retained locally under ignored artifacts. Static systemd
verification used separate temporary copies with ExecStart pointing to the
built executable; it emitted only an unrelated installed-unit legacy PIDFile
warning. No unit was loaded or activated.

The native payload now contains no Python backend/helper. The generated Bash
adapter preserves the pinned networking logic and uses Rust for all six recovery
operations. The original compatibility backend and desktop controls remain in
the untouched vendor tree. See [native recovery](native-recovery.md) for the
snapshot protocol, layout migration and operational limits.

The tests do not establish physical create_ap activation, actual privileged
dnsmasq behavior, driver compatibility, DHCP/DNS/NAT connectivity, real Polkit-agent
interaction, sleep/resume, installation or rollback. The real Bash networking
path was not started. The original project checkout was unchanged, and read-only
checks still showed the laptop connected and the installed daemon running with
its original process identity. No current network or service configuration was changed.
