# Native hostapd/dnsmasq runtime validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8,
dnsmasq 2.93 and systemd 261.3. Builds, tests and staging ran as an ordinary user
in the separate remote checkout. The actual dnsmasq executable was used only for
version reporting and a syntax-only `--test` invocation; no server was started.

## Exact candidate

- Implementation: `8de972c17c5453773846edc9c82c6e0ebb8bdc31`
- Source tree: `614cdd3d6d33fb741e9d04b0444176b128b7bec9`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `b67edbf5022676fd58a5f84efdb9030c4f982d6be87a6c011c96bc6f2b351419`
- Native Bash adapter SHA-256: `b1d9e7cc1e804e57481d4a82676f10cf285e36f38dc1dcbaa542f26aa8ba4682`
- create_ap staging manifest SHA-256: `556223b4f6f9869a61c78539f9cefb6ede0e808e44b1673f0d1887b0b6944837`
- NetworkManager staging manifest SHA-256: `500a8452ed178e41928c19de6eb09741ce49aeb0615c720e2d510ec8aeb0013e`

Git verified this exact revision, identical tracked content and no extra
untracked source before optimized validation. The subsequent documentation
commit records results only. Dependency versions and Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 40 passed |
| Full Python suite against optimized service, parity and child executables | 157 passed |
| Clippy with warnings denied and rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap staging | Seven files plus manifest |
| NetworkManager staging | Six files plus manifest |
| Generated Bash syntax | Passed |
| Both alternative service units, separate static verification | Passed |
| Installed dnsmasq 2.93 configuration/launch-option syntax check | Passed with --test; no activation |

There are 197 distinct test methods. Fourteen new daemon methods include
subcases for WPA1/2/3 transition/PSK rendering, Unicode/space-containing SSIDs,
radio flags, custom resolvers, hosts-file behavior, DNS/DHCP disabling, private
permissions, ownership manifests and changed configuration. The renderer is
compared to the pinned Bash template in six configurations with all external
commands stubbed. Comparison decodes the native SSID hex and accounts for the
intentional explicit AP interface binding in dnsmasq configuration.

Real disposable Rust child processes exercise gated launch, exact recorded PIDs,
foreground options, readiness, duplicate start refusal, output limits, normal
stdio closure, child failures, post-readiness exits and supervisor SIGKILL.
Recovery also succeeds after normal child-identity files are removed, using the
durable pre-exec boot/start journal. An unacknowledged or unrecorded gate cannot
execute. Readiness parsing has a new Rust unit case covering exact enabled state,
disabled/missing state, malformed text and duplicate state lines.

Lease tests cover private ownership/modes, reading the new lease location,
symlinks, foreign files, replacement directories and refusal to adopt a directory
after failed configuration. Plaintext credentials are absent from manifests,
child argv and returned errors. Fixtures use only temporary files and controlled
non-networking executables; missing fixture data cannot select a system daemon.

Two new desktop integration methods exercise startup failure for either daemon,
readiness timeout, and dnsmasq exit after Start succeeds. The unchanged desktop
methods report failure and clean owned interfaces, rules, leases and journals.
Existing client/signals, authorization, cancellation, supervisor-death and Stop
retry tests still pass. Upstream fixtures and foreign firewall rules survive.

## Evidence and limits

The build checkout retains `artifacts/validation-8de972c.log`,
`artifacts/create-ap-stage-8de972c/` and `artifacts/nm-stage-8de972c/`. The final log
and manifests are also retained locally under ignored artifacts. Static unit
verification used separate temporary copies with ExecStart pointing to the built
executable; an unrelated installed-unit legacy PIDFile warning was emitted.
No service was installed, enabled, loaded or restarted.

The generated adapter contains twenty Rust runtime calls. Bash no longer renders
hostapd/dnsmasq configuration, launches those daemons or polls AP readiness. It
retains option parsing, compatibility/radio checks, instance housekeeping,
transaction dispatch and optional module loading. The native payload contains no
Python backend/helper. See [daemon runtime details](native-daemons.md).

Live hostapd control sockets, actual dnsmasq privilege dropping and account
configuration, mandatory access-control policy, driver behavior, DHCP/DNS/NAT
connectivity and deployed upgrade/rollback remain hardware/deployment gates.
The new lease location requires distribution-policy review; no policy was
weakened or modified. A crash before lease-directory ownership is confirmed can
require operator review. These results are development validation, not release
certification. No physical hotspot activation occurred. The laptop's Wi-Fi
remained connected and its original installed daemon retained the same process.
The original project checkout and installed services were left unchanged.
