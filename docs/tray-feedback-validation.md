# Tray failure feedback validation

Validated on 2026-10-23 on Ubuntu 26.10 (Stonking Stingray), x86-64,
Python 3.14.8, GTK 3.24.52 and dpkg-deb 1.23.7.

The candidate was an isolated source snapshot based on
`6f2d7483d38e92f86eef593e50865c1daf08d282`, including the working-tree
changes and untracked tray/test sources. All 166 transferred source files
matched their SHA-256 manifest before testing. No existing remote checkout
was changed.

- Source manifest SHA-256: `0d3d7e01d07798b6133586231c3fde6035a4f6dad63c527d9aeefe246f2d3b79`.
- Source archive SHA-256: `d40763a3ec92ad2af9ea8a47ceaf45c97771f527cd6d7a8817a56042b5d6bace`.
- Tray SHA-256: `be0fb6f612a45ba55d43a01dbb5d0f6e1693407df8f2d628d109cd44f91e737d`.

## Results

All eight unittest cases passed without skips: seven real GTK menu checks
and one packaging test that assembles and inspects both NetworkManager and
create_ap packages. The tray checks cover saved service failures, channel
remediation, request failures surviving refresh, clearing errors on success,
transition labels, and explanations for disabled controls. Packaging checks
verify that both packages contain the maintained tray, launchers, login
configuration and required dependencies.

`tools/check-vendor.py` verified all 48 pinned files. Tests used a disposable
session bus, hidden GTK menus and a small existing ELF packaging fixture;
no Rust build or installed service binary was substituted or rebuilt.

No hotspot activation, Wi-Fi connection changes, route changes, networking
service restarts or package installation occurred during validation. Remote
NetworkManager device states were identical before and after testing. This
checks visible device states, not continuous packet loss or Wi-Fi performance.
Live hotspot activation and connected-client traffic remain untested by this run.
The unrelated Rust deployment changes present in the candidate were not tested
by this focused UI/package check.

This report was added after the tested source snapshot was captured. Raw logs,
source manifest and machine-readable results are retained outside Git under
`artifacts/tray-feedback-validation-20261023/` in the local workspace.
