# First Rust service milestone: validation

Validated on 21 October 2026 against source commit
`4510666c2e43c3aeea9c2739d6c393e24b14878b`, tree
`bcf9570c5858c97ec882b34b4099c46bb617d48a`. The checkout was clean before the
recorded build. This document was added afterward without runtime changes.

Environment: Ubuntu 26.10 (Stonking Stingray), reporting development branch,
x86-64; rustc 1.97.1; Cargo 1.97.1; Python 3.14.8 with PyGObject/Gio;
D-Bus 1.16.2. Compilation and automated tests ran on the remote development
machine. Local work was limited to editing, formatting and Git/source checks.

| Check | Result |
| --- | --- |
| `cargo build --release --locked --offline` | Passed; optimized executable produced |
| `cargo test --workspace --locked --offline` | 12 tests passed; no failures or ignored tests |
| `python3 tools/check-vendor.py` | All 48 reused files match the pinned Wi-Fi Relay snapshot |
| Private-bus Python tests, debug executable | 14 passed; no failures or skips |
| Private-bus D-Bus tests, optimized executable | All 9 D-Bus tests repeated successfully |
| Local rustfmt check | Passed |
| Git whitespace check | Passed for new source; immutable vendor bytes retain their upstream whitespace |

The D-Bus cases cover all ten method and three signal signatures, the original
desktop reply decoder and asynchronous client, real bus-assigned sender identity
in the Polkit request, authorization allow/deny and unavailable authority,
configuration rejection without mutation, Stop during startup, responsive status,
and refusal to replace an existing service owner. Polkit tests use a simulated
authority on the isolated bus, not the host's system authority or an auth agent.

Rust subprocess tests cover bounded/truncated output, transport failure,
acknowledging observations before replies, rejected operations, failed cleanup
reporting, and closing stdin to deliver actual EOF. Python adapter tests use the
pinned GLib scheduler with a fake networking engine to check RPC values,
cancellation, malformed input without credential echo, parent EOF and closed
output pipes without interrupting cleanup.

Python test commands used `-W ignore::DeprecationWarning` to suppress installed
PyGObject/asyncio compatibility deprecations. Clippy and remote rustfmt were
unavailable; no packages were installed to obtain them. Rust formatting was
checked locally. The dependency cache initially lacked tokio-macros; Cargo
resolved/downloaded dependencies once before the final locked, offline checks.

Retained artifacts are outside Git under `artifacts/` on the build checkout:

- `validation-4510666.log`: source/tree IDs, environment, release build, Rust
  tests, provenance check and debug integration tests.
- `release-dbus-4510666.log`: optimized-service integration checks.
- `relayforge-service-4510666-linux-x86_64`: built executable, SHA-256
  `16b0281ba7d2fe310512b5226cff4dfa7e5fb090013e9fcda4b2fa8ccd19e1df`.

Cargo.lock SHA-256:
`a29d1c58922aa6c578f50e6563e736389e9f71d022327162a2e3dfaf63e86523`.
The raw logs are also retained with the local project under `artifacts/`.

These results establish isolated protocol and simulated lifecycle behavior.
They do not establish live create_ap or NetworkManager operation through the new
adapter, real privileged authentication, GTK rendering, GNOME Shell loading,
hardware networking, crash recovery of actual interfaces/firewall rules, or
device suspend behavior. No service was installed and no physical network was
modified. The backend migration and deployment gates remain in
[architecture.md](architecture.md).
