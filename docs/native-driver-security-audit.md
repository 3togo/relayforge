# Native driver and security audit — 2026-10-23

Task 15 audits the pinned Wi-Fi Relay source at
`12f9f78e2cf4e9f41abacf94fbf5f5524bf47814`, especially
`vendor/wifi-relay/daemon/create_ap`. This is a contract audit, not adapter
certification. Task 16 enables only paths with an implemented native contract;
unknown vendor hostapd interfaces remain refused.

| Reference branch | Native decision and evidence |
| --- | --- |
| Default hostapd `nl80211` | Keep nl80211 for cfg80211 AP-capable drivers. iw AP/concurrency/channel observations and interface identity checks remain mandatory; module names alone do not prove hardware support. |
| `brcmfmac` virtual-interface restriction (lines 317–327) | Preserve refusal; explain a dedicated adapter with `NO_VIRT=1` and a separate upstream before helper launch. Dedicated-adapter ownership/restoration is covered offline; physical qualification is open. |
| `8192[cd][ue]` and `8723a[sue]` select patched `rtl871xdrv` (1485–1494) | Refuse all seven exact module names before startup mutation. No binary-string heuristic, arbitrary driver argument or hostapd replacement. A verified patched hostapd contract and representative captures are still required. |
| `rtl[0-9]*` rewrites WPA to WPA2 (1609–1615) | Never rewrite security. Keep an actionable WPA1 refusal for that exact numeric prefix. Correct the previous broader `rtl*` match so unrelated names are not classified as that branch. Modern `rtl8192ce` and `rtl8xxxu` remain nl80211 paths with explicit supported security. |
| WPA1/2 (1839–1847) | Preserve requested version and CCMP; no automatic TKIP, open-network, WEP or WPA fallback. Raw PSK remains available for WPA1/2. |
| `WPA_VERSION=3` (1826–1837) | Retain WPA2/WPA3 transition behavior: PSK plus SAE and optional PMF. Capabilities and Settings explicitly identify allowed WPA2 clients. This setting does not request WPA3-only mode. |
| Raw PSK with transition mode | Refuse before helper launch and frozen runtime setup: it supplies no SAE password. No fallback to WPA2 or reinterpretation of a PSK as a new password. The schema still reads old settings for diagnosis/editing and ownership-based cleanup. |
| NetworkManager native backend | Retains its existing WPA2/shared-profile contract and refuses unsupported security/radio overrides. It does not consume a hostapd vendor-driver choice. NM-owned/helper-owned child ownership and observed concurrency remain separate provider contracts requiring hardware qualification. |
| Regulatory/MAC overrides, PPTP modules, global firewall workarounds | Retain task 17's explicit provisioning boundary. No implicit country, parent-MAC, module or global firewall changes. |

The shared policy is in `relayforge-core/src/ap_policy.rs`. GetCapabilities and
Start apply it before helper launch. The runtime repeats it before radio-baseline
creation, rendering and final daemon exec. The baseline binds module, parent,
PHY, configuration and upstream association. Observed driver changes invalidate
startup; these checks do not atomically exclude another administrator's actions.

Security capabilities contain policy fields only, never credentials or SSIDs.
They do not probe hostapd's compiled SAE support or report negotiated security.
Hostapd readiness and isolated client testing remain required. Stop/recovery do
not depend on the new startup policy, so existing owned resources remain cleanable.

## Primary contracts

[Linux Wireless hostapd documentation](https://wireless.docs.kernel.org/en/latest/en/users/documentation/hostapd.html)
identifies nl80211 for cfg80211 drivers implementing AP support. The
[kernel rtl8192ce source](https://raw.githubusercontent.com/torvalds/linux/master/drivers/net/wireless/realtek/rtlwifi/rtl8192ce/sw.c)
is distinct from the reference's exact `8192ce` vendor name.

The [hostapd configuration reference](https://android.googlesource.com/platform/external/wpa_supplicant_8/+/refs/heads/master/hostapd/hostapd.conf)
documents passphrase/PSK inputs, SAE's passphrase default, and SAE-only mode with
required PMF. These support the raw-PSK refusal and explicit mixed-mode label.

## Remaining gates

The source/policy audit is complete. Standard nl80211 and dedicated brcmfmac
software paths, early errors, transition-mode presentation and credential handling
have fixture coverage. Vendor-driver enablement, actual SAE negotiation,
per-driver traffic/cancellation/recovery, regulatory policy and global firewall
integration remain open. Software fixtures do not replace representative driver
captures or a dedicated radio bench. See `native-driver-security-validation.md`.
