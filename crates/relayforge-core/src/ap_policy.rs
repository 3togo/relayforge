//! Native nl80211 policy; descriptions never contain credential material.
use crate::config::{self, Config};
use serde::Serialize;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Security {
    pub mode: &'static str,
    pub wpa: &'static str,
    pub key_management: &'static str,
    pub protected_management_frames: u8,
    pub pairwise_cipher: &'static str,
    pub supports_raw_psk: bool,
}

pub fn security(config: &Config) -> Result<Security, &'static str> {
    let config = config::validate(config)?;
    let (mode, wpa, key_management, pmf) = match config["WPA_VERSION"].as_str() {
        "1" => ("wpa1-personal", "1", "WPA-PSK", 0),
        "2" => ("wpa2-personal", "2", "WPA-PSK", 0),
        "3" => {
            if config["USE_PSK"] == "1" {
                return Err("WPA3 transition mode requires a passphrase for SAE; disable raw PSK or select WPA2 explicitly");
            }
            ("wpa2-wpa3-transition", "2", "WPA-PSK SAE", 1)
        }
        _ => return Err("Unsupported native security mode"),
    };
    Ok(Security {
        mode,
        wpa,
        key_management,
        protected_management_frames: pmf,
        pairwise_cipher: "CCMP",
        supports_raw_psk: pmf == 0,
    })
}

pub fn driver(module: &str, config: &Config) -> Result<(), &'static str> {
    if module.is_empty()
        || module.len() > 128
        || !module
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err("Invalid radio driver identity");
    }
    let config = config::validate(config)?;
    security(&config)?;
    if module == "brcmfmac" && config["NO_VIRT"] == "0" {
        return Err("Native virtual interfaces are unavailable for brcmfmac; select a dedicated adapter with NO_VIRT=1 and a separate upstream");
    }
    if [
        "8192cu", "8192ce", "8192du", "8192de", "8723as", "8723au", "8723ae",
    ]
    .contains(&module)
    {
        return Err("Legacy radio driver requires unsupported rtl871xdrv hostapd; use a supported nl80211 driver or adapter");
    }
    // The reference rewrites rtl[0-9]* to WPA2. Keep an explicit refusal for
    // that unqualified combination, without rejecting unrelated rtl* modules.
    if module.starts_with("rtl")
        && module.as_bytes().get(3).is_some_and(u8::is_ascii_digit)
        && config["WPA_VERSION"] == "1"
    {
        return Err("Select WPA2 explicitly for this radio driver");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn security_modes_are_explicit_and_do_not_expose_credentials() {
        for (version, mode, wpa, akm, pmf) in [
            ("1", "wpa1-personal", "1", "WPA-PSK", 0),
            ("2", "wpa2-personal", "2", "WPA-PSK", 0),
            ("3", "wpa2-wpa3-transition", "2", "WPA-PSK SAE", 1),
        ] {
            let mut c = config::defaults();
            c.insert("WPA_VERSION".into(), version.into());
            let p = security(&c).unwrap();
            assert_eq!(
                (
                    p.mode,
                    p.wpa,
                    p.key_management,
                    p.protected_management_frames
                ),
                (mode, wpa, akm, pmf)
            );
            assert!(!serde_json::to_string(&p)
                .unwrap()
                .contains(&c["PASSPHRASE"]));
        }
    }
    #[test]
    fn raw_psk_cannot_silently_remove_sae() {
        let mut c = config::defaults();
        c.insert("USE_PSK".into(), "1".into());
        c.insert("PASSPHRASE".into(), "a".repeat(64));
        for version in ["1", "2", "3"] {
            c.insert("WPA_VERSION".into(), version.into());
            let before = c.clone();
            assert_eq!(security(&c).is_ok(), version != "3");
            assert_eq!(c, before);
        }
    }
    #[test]
    fn vendor_contracts_and_dedicated_broadcom_do_not_change_settings() {
        let mut c = config::defaults();
        for module in [
            "8192cu",
            "8192ce",
            "8192du",
            "8192de",
            "8723as",
            "8723au",
            "8723ae",
            "brcmfmac",
            "../driver",
            "bad\nmodule",
            "",
        ] {
            assert!(driver(module, &c).is_err(), "{module}");
        }
        c.insert("NO_VIRT".into(), "1".into());
        c.insert("INTERNET_IFACE".into(), "eth0".into());
        let before = c.clone();
        assert!(driver("brcmfmac", &c).is_ok());
        assert_eq!(c, before);
    }
    #[test]
    fn nl80211_modules_are_not_confused_with_legacy_vendor_names() {
        let mut c = config::defaults();
        for module in [
            "ath9k",
            "mt7921e",
            "iwlwifi",
            "rtw88_8723de",
            "rtl8192ce",
            "rtl8xxxu",
        ] {
            assert!(driver(module, &c).is_ok());
        }
        c.insert("WPA_VERSION".into(), "1".into());
        assert!(driver("rtl8192ce", &c).is_err());
        assert!(driver("rtl_test", &c).is_ok());
    }
}
