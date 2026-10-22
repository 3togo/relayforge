# Native interface creation and NetworkManager ownership validation

Validated on 2026-10-22 using Ubuntu 26.10 (Stonking Stingray, reporting development
branch), x86_64, Rust/Cargo 1.97.1, rustfmt 1.9.0, Clippy 0.1.97, Python 3.14.8
and systemd 261.3. Builds, automated checks and staging ran as an ordinary user in
the separate remote development checkout. No real interface was created, no
system-bus NetworkManager device was modified, and no service was activated.

## Exact candidate

- Implementation: `f6181d03c2d0905313fbee510c9965f4f0194d36`
- Source tree: `ec82a3294e494be5fc7f7c4b6361a0282cbff66f`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service SHA-256: `4adfe48f8264ebd10a2ccb42c2fbfbaea06bfcaf889be541e10a151423c0e477`
- Native Bash adapter SHA-256: `d20de8da97b47324525489e08c166414ec474ca736b390091198f783fb9425dd`
- create_ap staging manifest SHA-256: `d633992f1573fdac9255e787be55bd13f15940ecf37087d12370751225611767`
- NetworkManager staging manifest SHA-256: `a949340b67bfd0e18c81e379ccb274a36936696a8e7745261823806c6a8115f0`

Git verified this exact source revision, identical tracked content and no extra
untracked source before optimized validation. The subsequent documentation commit
records results only. Cargo.lock and dependency versions are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 39 passed |
| Full Python suite against optimized service, parity and child executables | 141 passed |
| Clippy with warnings denied and rustfmt | Passed |
| Vendor integrity | All 48 pinned files unchanged |
| create_ap staging | Seven files plus manifest |
| NetworkManager staging | Six files plus manifest |
| Generated Bash syntax | Passed |
| Both alternative service units, separate static verification | Passed |

There are 180 distinct test methods. Twenty-one new device methods cover intent
and snapshot persistence, structured creation arguments, MAC/name selection,
idempotency, allocation locking, lost/failed creation replies, release/restore
failures and lost replies, originally unmanaged dedicated adapters, later
connections, dead owners, renamed/reused devices, radio changes, unsafe journals,
historical ownership and a missing NetworkManager/device.

Private-session-bus tests execute the actual Rust D-Bus property path against a
small simulated NetworkManager. They check exact Managed boolean values, per-device
targeting, original-state restoration, virtual-device cleanup, query failure,
request timeout, denial, a reply lost after mutation, and changed unique service
owners. A query failure cannot establish absence, and none of these tests opens
the system bus. Other cases ensure a reclaimed or activating AP blocks setup and
removal while preserving the upstream fixture.

The generated creation block is also executed with its Rust runtime calls
redirected to the private fixture entry point. That verifies shell assignments,
subsequent snapshot saving, release, setup and cleanup together. It never runs
the complete generated networking script or live kernel utilities.

The existing non-networking lifecycle engine now uses Rust for actual simulated
interface creation and NetworkManager release/restoration. A new desktop startup
test injects creation/release failures and lost replies, then verifies Stop leaves
no owned interface, manager device or supervisor journal. Existing desktop-client,
configuration, authorization, cancellation, supervisor-death and cleanup-retry
coverage continues to pass. Fixture state cannot fall back to live networking.

## Evidence and limits

The build checkout retains `artifacts/validation-f6181d0.log`,
`artifacts/create-ap-stage-f6181d0/` and `artifacts/nm-stage-f6181d0/`. The final log
and manifests are also retained locally under ignored artifacts. Static unit
verification used separate temporary copies with ExecStart pointing to the built
executable; an unrelated installed-unit legacy PIDFile warning was emitted.
No unit was installed, loaded, enabled or restarted.

The generated native adapter has eighteen Rust runtime calls. It contains no
Bash allocator, unmanaged-device helper, direct interface-add command, networking
sysctl access or direct iptables command. Rust now owns interface creation,
NetworkManager release/restoration, AP setup/teardown, forwarding and firewall
transactions. Bash still parses options, configures/launches daemons and loads
optional modules. Rust still uses bounded iw/ip utilities for kernel operations.
See [device ownership details](native-device-ownership.md) for protocol and API
compatibility boundaries, including the nonpersistent Managed property.

These checks do not establish physical driver/udev behavior, actual
NetworkManager policy or Polkit behavior for device changes, DHCP/DNS/NAT
connectivity, or deployed upgrade/rollback. Live activation remains deferred.
The laptop's Wi-Fi remained connected, and its original installed daemon
remained active with the same process throughout. The original project checkout
and installed services were left unchanged.
