# Native radio validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.
Builds and automated tests ran in the separate remote development checkout.

- Implementation: `d2cdb0c06654bd2a641a6a2b8edf0ba9ed6a18e9`
- Source tree: `33938137ae8ef3c51a5c2ee3faf191f521a967cf`
- Cargo.lock SHA-256: `84b72fa4915a5f4bed40bc217cc6e4891592f7f27d0bacacb557770d6a86d81c`
- Optimized Linux x86_64 executable SHA-256:
  `ebd7a232eecc13cd7210f3608347ee06f182b88ebb8c278426eb45e4fdb44e73`

The remote source was synchronized with the committed candidate, including new
files. Git verified identical tracked content, no extra untracked source files
and a clean checkout before the final recorded run. The executable was built
from the same runtime sources; the final integration run used the optimized
executable explicitly through `RELAYFORGE_BINARY`.

All checks passed:

| Check | Result |
| --- | --- |
| Offline locked workspace/all-targets build | Passed |
| Rust workspace/all-targets tests | 32 passed |
| Python parity, private-bus compatibility and worker tests | 30 passed |
| Python suite using optimized service | 30 passed |
| Optimized locked offline workspace build | Passed |
| rustfmt | Passed |
| Clippy, all targets, warnings denied | Passed |
| Vendor integrity | All 48 pinned files unchanged |

The 62 distinct tests include full capability-object comparison with the pinned
Python daemon across 32 ordinary radio captures, stricter unknown-restriction and
duplicate-entry behavior, malformed identities/link state, concurrency capacities,
channel policy and NetworkManager configuration checks. Process tests cover
bounded stdout/stderr, invalid UTF-8, sanitized command errors, timeouts with
inherited pipes and cancellation that kills a process and its descendant.
Private-bus tests cover native capability replies, rejected startup without
activation, continued status/Stop availability and removal of capability requests
from the Python worker protocol. Existing configuration, authorization, signals,
cancellation and unmodified desktop-client tests also pass.

Retained ignored artifacts are `artifacts/validation-d2cdb0c.log`,
`artifacts/native-radio-final-checks.log`, and the optimized executable
`artifacts/relayforge-service-d2cdb0c-linux-x86_64` in the build checkout. Validation
logs are also retained locally. The documentation commit following the tested
implementation only records results.

No system-bus deployment, graphical desktop session, physical Wi-Fi operation,
regulatory-domain change, firewall change or Android test was performed. Fixture
results do not establish driver/channel behavior or end-to-end connectivity on
physical hardware. Native parsing still consumes external iw output. Python
continues to own live revalidation, activation, upstream band switching,
NetworkManager profile/sibling checks, ownership journals, recovery and cleanup.
