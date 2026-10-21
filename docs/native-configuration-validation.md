# Native configuration migration: validation

Validated on 21 October 2026 against implementation commit
`55fb33974790c5a172bf1c44f61c713f6714e722`, tree
`6fb7fd40f026663f8c3c33e3cf8be93f970c6af7`, on branch
`backend/native-config`. Both source checkouts matched that revision and the
build checkout was clean before and after the recorded checks. This report and
documentation links were added afterward without runtime changes.

Verified build/test environment: Ubuntu 26.10 (Stonking Stingray), reporting
development branch; x86-64; rustc/Cargo 1.97.1; Python 3.14.8 with PyGObject/Gio;
D-Bus 1.16.2. Automated builds and tests ran on the remote development machine.
Clippy 0.1.97 and rustfmt 1.9.0 were available in this run. No packages or host
services were installed or changed. Cargo downloaded the newly added, pinned
unicode-general-category dependency once; final checks used the locked cache.

| Executed check | Result |
| --- | --- |
| `cargo build --workspace --all-targets --locked --offline` | Passed; debug service and differential-test example built |
| `cargo build --release --workspace --all-targets --locked --offline` | Passed; optimized service and examples built |
| `cargo test --workspace --locked --offline` | 23 Rust tests passed; no failures or ignored tests |
| Private-bus Python integration/parity suite with optimized service | 23 passed; no failures or skips |
| `python3 tools/check-vendor.py` | All 48 files match upstream snapshot `12f9f78e2cf4e9f41abacf94fbf5f5524bf47814` |
| `cargo fmt --all -- --check` | Passed remotely; local formatting check also passed |
| `cargo clippy --workspace --all-targets --locked --offline -- -D warnings` | Passed |
| Git source/whitespace checks | Passed; tested source was clean |

The 23 Python tests comprise 14 D-Bus/client cases, six compatibility-worker
cases, and three configuration parity cases. They exercise the unchanged desktop
decoder and asynchronous client, exact ten-method/three-signal signatures,
Polkit subjects/denial/unavailable authority, persistent native saves and restart,
merged PSK validation, settings edits during startup cancellation, active backend
switch rejection, malformed external configuration, and removed worker requests.

Schema parity compares normalized results and acceptance against the immutable
Python reference for field, cross-field and generated combined cases. The Unicode
comparison checks all 1,114,112 code points against Python's isprintable behavior.
Rust storage tests cover private random initialization, no-overwrite semantics,
atomic replacement and password retention, rejected updates, stale external edits,
symlink/hardlink and ownership/mode policies, FIFO/oversized/non-UTF-8 input,
temporary-file cleanup, and refusal to use a historical password when the stored
file lacks PASSPHRASE. Interface inventory uses a disposable sysfs-shaped fixture.

Retained build artifacts, outside Git under artifacts/:

- `validation-55fb339.log`: source/tree IDs, environment, builds, Rust tests,
  optimized-service Python checks and provenance verification; SHA-256
  `2361fdb49fd77792e1af67e341913aaac8bc39ce5d7dd47d0da13f2db02544a4`.
- `static-checks-55fb339.log`: verified formatter/Clippy versions and checks.
- `relayforge-service-55fb339-linux-x86_64`: optimized executable; SHA-256
  `4947a37d9a3157b4d103f0d57a456e59f51cb1f8c60eaa50d23bb121d3cb0aa1`.

Cargo.lock SHA-256:
`84b72fa4915a5f4bed40bc217cc6e4891592f7f27d0bacacb557770d6a86d81c`.
Raw validation/static-check logs are retained with both source checkouts.
The Python command suppresses installed PyGObject/asyncio DeprecationWarning
messages with `-W ignore::DeprecationWarning`.

These checks validate native configuration, filesystem behavior, simulated
lifecycle ordering and desktop D-Bus compatibility. They do not validate a real
root/system-bus deployment, real Polkit authentication agents, GTK rendering,
GNOME Shell loading, host adapter discovery, create_ap/NetworkManager traffic,
physical crash recovery, suspend behavior or power-loss durability. No hotspot
was started and no installed configuration was changed. Capability inspection,
network activation and resource recovery still use the pinned Python backend.
