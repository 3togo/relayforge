# Native NetworkManager validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8
and systemd 261.3. Builds, automated tests and staging ran in the separate remote
development checkout. The laptop's installed daemon and active Wi-Fi were not
changed or used for activation.

## Exact candidate

- Implementation: `10f05c0ce667417ff480523da48d9bc16db03116`
- Source tree: `18ee45c449ecae57a1da5f0d86956c358f59b8d1`
- Cargo.lock SHA-256:
  `84b72fa4915a5f4bed40bc217cc6e4891592f7f27d0bacacb557770d6a86d81c`
- Optimized Linux x86_64 executable SHA-256:
  `30f466285b3194a9d874e87d0d6453f1ab4828ccf9117a0aa0a4792a81143843`
- Staged payload manifest SHA-256:
  `15706337d8aa8ca0534abc08e977eb8bbf4679af961b73034315b401463e162c`

The complete candidate, including new files, was synchronized to the build
checkout. Git verified identical tracked content, no extra untracked source and
a clean checkout at the recorded revision. The subsequent documentation commit
only records these results.

## Results

| Check | Result |
| --- | --- |
| Locked offline workspace/all-targets build | Passed |
| Rust workspace/all-targets tests | 33 passed |
| Python private-bus, parity, worker and staging tests against optimized service | 50 passed |
| rustfmt | Passed |
| Clippy, all targets, warnings denied | Passed |
| Optimized locked offline workspace build | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| Native payload staging | Six payload files plus hash/mode manifest |
| systemd unit static verification | Passed with ExecStart pointing to the built executable |

There are 83 distinct test methods. The 15 native-backend tests include additional
subcases for both interface paths and multiple injected failures. Tests cover
activation/profile settings, AP-mode restoration, authorized clients, cancellation,
ambiguous activation reply timeout, partial interface creation, denied/failed
activation, occupied PHYs, subnet overlap, upstream changes, process death,
NetworkManager restart, SIGTERM cleanup, interface-name reuse, changed profile
ownership and cleanup retry. The unmodified asynchronous desktop client receives
native status and client signals. The fixture also checks that the upstream is
never modified or deleted and that no Python networking child is spawned.

Journal tests exercise locking, validated records, atomic round trips, rejection
of external edits and symlinks. Existing configuration, credential storage,
Unicode parity, authorization, cancellation and legacy-worker checks continue
to pass. Configuration initialization can select NetworkManager without changing
an existing file. External backend-setting changes are rejected before native
activation. A failed cleanup remains visibly failed despite late observations;
Start remains blocked until Stop confirms cleanup.

Staging was performed from the actual optimized executable into a new ignored
artifact directory. It did not install files or create configuration. The staged
payload contains the native executable, systemd unit, bus activation file,
original bus and Polkit policies, and MIT license. Static unit verification used
an otherwise identical copy with ExecStart changed to the build artifact path;
no service was loaded or started. The verifier returned zero and emitted an
unrelated existing-unit legacy PIDFile warning. Installed activation, root
ownership, Polkit-agent interaction and rollback were not tested.

## Retained evidence and limits

The build checkout retains `artifacts/validation-10f05c0.log`,
`artifacts/unit-validation-10f05c0.log`, and the reviewable payload at
`artifacts/native-stage-10f05c0/`. Validation logs and the staged manifest are also
retained locally under ignored artifacts. Credentials and machine-specific
configuration are excluded from the payload and repository.

The fake NetworkManager and simulated kernel validate protocol and lifecycle
behavior; they do not establish physical AP creation, real driver compatibility,
DHCP/DNS/NAT connectivity, throughput, sleep/resume or distribution installation.
No native service was deployed, installed or enabled. The laptop was still
connected and its installed daemon remained running after the checks.

Native mode is opt-in and requires BACKEND=networkmanager. Band switching is
declined to preserve upstream. Automatic resume and seamless switching between
native and compatibility service modes remain future work. create_ap supervision
and the existing compatibility engine still use Python; native mode does not
construct that worker. See the [native mode guide](native-networkmanager.md) for
operational boundaries and the staged migration/rollback procedure.
