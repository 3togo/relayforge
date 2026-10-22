# Native backend audit validation

Validated on 2026-10-22 as an ordinary user in a separate remote checkout:
Ubuntu 26.10 (Stonking Stingray, stable), x86_64, Rust/Cargo 1.97.1,
rustfmt 1.9.0, Clippy 0.1.97 and Python 3.14.8.

## Exact source and artifacts

- Implementation: `f902c34763c4dec9434b32c58bcfb48dcc2d6c4e`
- Source tree: `aed6e2a2562333865c8df44e91b9f82b1913d099`
- Cargo.lock SHA-256: `62bd337d8d0722fd2e93214f23695c187e8d9c26309a1e90b34fe0d6c4da9f5f`
- Optimized service and native helper SHA-256: `263d577992ee7e5eff26ef06fc5087a29c207bc0033e60edf8ad949b1111b013`
- create_ap staging manifest SHA-256: `104494ee8a44f9017abe19da799d6f2e1b053aac4d5359fd0a5abce7c2fd0410`
- NetworkManager staging manifest SHA-256: `c98ccfa1d6ea5dce526b2c5044431a3c37ce066ad9d2fd288c3ef313a52fbbdf`

The remote checkout matched this revision without tracked differences or extra
untracked source before validation, and remained clean afterward. The subsequent
documentation commit records results only. Dependencies and Cargo.lock are unchanged.

## Results

| Check | Result |
| --- | --- |
| Locked offline optimized workspace/all-targets build | Passed |
| Optimized Rust workspace/all-targets tests | 42 passed |
| Full Python suite against optimized service and examples | 181 passed |
| Clippy with warnings denied; rustfmt | Passed |
| Vendor integrity | Passed |
| create_ap and NetworkManager staging | Passed |
| Native helper/service byte comparison | Identical |

All 223 test methods passed. New coverage checks broad, narrow, host and malformed
connected routes, including a collision arriving after interface creation but
before address setup. Rejected setup preserves simulated upstream state.

Retirement fault cases interrupt proof creation, marker deletion, ordinary file
deletion, snapshot deletion and directory removal. Recovery resumes each case
without replaying network changes. Foreign or changed files, replaced identities,
changed markers and malformed completion records remain protected. A private-bus
desktop Start/Stop regression confirms that Stop can retry after the instance
directory is already gone. Existing desktop contract, asynchronous client,
authorization, lifecycle, cancellation and recovery tests also pass.

## Evidence and limits

Ignored artifacts retain `artifacts/validation-f902c34.log`,
`artifacts/create-ap-stage-f902c34/` and `artifacts/nm-stage-f902c34/`.
The final log and both manifests are retained locally as well.

No packages were installed and no service was installed, enabled, loaded or
restarted. No physical hotspot was activated. Read-only local checks found the
Wi-Fi interface connected, the original daemon retaining its process, and the
original project checkout clean.

See the [audit](backend-audit.md) for fixed findings and remaining migration
boundaries. Automatic resume/suspend parity, distribution policy integration,
legacy credential rotation and installer repair remain separate work. Live
drivers, hostapd/dnsmasq, DHCP/DNS/NAT, mandatory access control and deployment
upgrade/rollback still require controlled validation. Offline results do not
certify physical networking or production readiness.
