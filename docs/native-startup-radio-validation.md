# Native startup radio policy validation

Validated on 2026-10-22 as an ordinary user in the separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, reporting development branch), x86_64,
Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `f3e59bd106fdaa7b22fa811f49b91432e7a6af61`
- Source tree: `e79a516794a66d234d735288d7923b20cd92e24a`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `77fae57cdeab303518d65632f7b3454feb123ac3e3e77d62b7931f8e9934d4bc`
- Native adapter SHA-256: `299f2fb0ed3f610e5313c101afdb3b6d5ea53c6336a53a90e44b5cdf6fa98771`
- create_ap staging manifest SHA-256: `acf12d30d8d8aa3641138d5c6b5b2dbf7157e67ef0716cffaa6b3761b3f8e7da`
- NetworkManager staging manifest SHA-256: `ac1aff45021ee7b7c32812fe2a69434cb650f1cde3173eae08a47b4adb445186`

The checkout matched this commit with no tracked differences or extra untracked
source before final validation and after the checks. The subsequent commit
records documentation only. Dependencies and Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 40 passed |
| Full Python suite against optimized service and examples | 165 passed |
| Clippy with warnings denied; rustfmt check | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap and NetworkManager staging | Seven and six payload files, plus manifests |
| Generated native Bash syntax | Passed |

All 205 distinct test methods passed. Seven new capture-based methods cover
private idempotent baselines, absence of literal credentials/upstream identities,
restricted channels, invalid mode/PHY/link output, concurrency, legacy driver
refusals, explicit dedicated-adapter handling, frozen configuration changes,
roaming, reused parents, unsafe or missing files, dead owners and refusal to
launch children after a late roam. They assert no simulated network mutation.

The generated-adapter integration test now executes the real Rust radio check
before interface creation, with private fixtures. A new private-bus desktop
test confirms a radio-driver rejection reaches Start and Stop cleans the owned
runtime without any simulated network mutation. Existing desktop methods,
signals, authorization, lifecycle failure/recovery and child tests remain green.
The installed dnsmasq parser is exercised only with its syntax-only --test flag.

## Evidence and limits

Ignored build artifacts retain `artifacts/validation-f3e59bd.log`,
`artifacts/create-ap-stage-f3e59bd/` and `artifacts/nm-stage-f3e59bd/`. The final log
and both manifests are also retained locally. No packages were installed and no
system service was installed, enabled, loaded or restarted.

The native adapter now contains 21 Rust runtime calls. Bash retains option
parsing, config loading, instance locking/markers, lifecycle dispatch and
filesystem housekeeping. Radio/driver/channel policy is Rust-owned; optional
PPTP module loading is removed rather than replaced. See
[startup radio policy](native-startup-radio.md) for supported behavior.

These offline tests do not validate actual drivers, hostapd sockets, dnsmasq
privilege dropping, mandatory access-control policy, DHCP/DNS/NAT connectivity
or deployed upgrade/rollback. No physical hotspot was activated. Read-only local
audits before and after the work showed the Wi-Fi interface connected and the
original installed daemon retaining the same process. The original project
checkout remained clean and installed networking was untouched.
