//! Validate desktop configuration patches before crossing the worker boundary.
//! Full merged configuration is validated again by the pinned compatibility backend.
use std::collections::BTreeMap;
use std::net::Ipv4Addr;

pub type Config = BTreeMap<String, String>;
pub const MAX_CONFIG: usize = 65536;

pub fn parse_patch(input: &str) -> Result<Config, &'static str> {
    if input.len() > MAX_CONFIG {
        return Err("Configuration exceeds 64 KiB");
    }
    // Preserve duplicate-key rejection, including escaped spellings of a key.
    struct Unique;
    impl<'de> serde::de::Visitor<'de> for Unique {
        type Value = Config;
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("unique string configuration keys")
        }
        fn visit_map<M: serde::de::MapAccess<'de>>(self, mut map: M) -> Result<Config, M::Error> {
            let mut result = Config::new();
            while let Some((k, v)) = map.next_entry::<String, String>()? {
                if result.insert(k, v).is_some() {
                    return Err(serde::de::Error::custom("duplicate key"));
                }
            }
            Ok(result)
        }
    }
    use serde::de::Deserializer;
    let mut decoder = serde_json::Deserializer::from_str(input);
    let config = decoder
        .deserialize_map(Unique)
        .map_err(|_| "Expected an object of unique string keys and values")?;
    decoder
        .end()
        .map_err(|_| "Unexpected trailing configuration data")?;
    for (key, value) in &config {
        validate_field(key, value)?;
    }
    Ok(config)
}

fn validate_field(key: &str, v: &str) -> Result<(), &'static str> {
    if v.chars().count() > 4096
        || v.chars()
            .any(|c| c.is_control() || matches!(c, '\u{2028}' | '\u{2029}'))
    {
        return Err("Configuration values must be printable single-line text");
    }
    let valid = match key {
        "WIFI_IFACE" | "INTERNET_IFACE" => {
            !v.is_empty()
                && v.len() <= 15
                && v.as_bytes()[0].is_ascii_alphanumeric()
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
        }
        "SSID" => !v.is_empty() && v.len() <= 32,
        "PASSPHRASE" => {
            v.is_ascii()
                && ((8..=63).contains(&v.len())
                    || (v.len() == 64 && v.bytes().all(|b| b.is_ascii_hexdigit())))
        }
        "BACKEND" => matches!(v, "create_ap" | "networkmanager"),
        "FREQ_BAND" => matches!(v, "auto" | "2.4" | "5"),
        "CHANNEL" => {
            v == "default"
                || (v.len() <= 3
                    && v.bytes().all(|b| b.is_ascii_digit())
                    && v.parse::<u16>().is_ok_and(|n| (1..=196).contains(&n)))
        }
        "WPA_VERSION" => matches!(v, "1" | "2" | "3"),
        "SHARE_METHOD" => matches!(v, "nat" | "bridge" | "none"),
        "GATEWAY" => v.parse::<Ipv4Addr>().is_ok_and(|ip| {
            !ip.is_multicast()
                && !ip.is_unspecified()
                && !ip.is_loopback()
                && !matches!(ip.octets()[3], 0 | 255)
        }),
        "DHCP_DNS" => v == "gateway" || v.split(',').all(|s| s.parse::<Ipv4Addr>().is_ok()),
        "MAC_FILTER_ACCEPT" => {
            v.starts_with("/etc/hostapd/")
                && v.len() > 13
                && !v.split('/').any(|s| s == "..")
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_./-".contains(&b))
        }
        "COUNTRY" => v.len() == 2 && v.bytes().all(|b| b.is_ascii_uppercase()),
        "ETC_HOSTS" | "NO_DNS" | "NO_DNSMASQ" | "HIDDEN" | "MAC_FILTER" | "ISOLATE_CLIENTS"
        | "IEEE80211N" | "IEEE80211AC" | "IEEE80211AX" | "NO_VIRT" | "USE_PSK" => {
            matches!(v, "0" | "1")
        }
        _ => return Err("Unsupported configuration key"),
    };
    if valid {
        Ok(())
    } else {
        Err("Invalid configuration value")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prevents_config_and_command_injection() {
        for value in [
            r#"{"SSID":"x\nBACKEND=evil"}"#,
            r#"{"WIFI_IFACE":"--help"}"#,
            r#"{"MAC_FILTER_ACCEPT":"/etc/hostapd/../passwd"}"#,
            r#"{"UNKNOWN":"value"}"#,
            r#"{"SSID":"one","\u0053SID":"two"}"#,
            r#"{"SSID":"one"}{}"#,
        ] {
            assert!(parse_patch(value).is_err(), "{value}");
        }
    }
    #[test]
    fn validates_radio_and_credentials() {
        assert!(parse_patch(r#"{"SSID":"测试","CHANNEL":"12","FREQ_BAND":"2.4"}"#).is_ok());
        for value in [
            r#"{"CHANNEL":"0"}"#,
            r#"{"GATEWAY":"192.168.1.255"}"#,
            r#"{"PASSPHRASE":"short"}"#,
            r#"{"HIDDEN":"true"}"#,
        ] {
            assert!(parse_patch(value).is_err());
        }
        assert!(parse_patch(&format!("{{\"SSID\":\"{}\"}}", "测".repeat(11))).is_err());
    }
}
