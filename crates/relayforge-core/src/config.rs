//! Complete Wi-Fi Relay configuration schema. No credentials are included in errors.
use std::collections::BTreeMap;
use std::net::Ipv4Addr;
use unicode_general_category::{get_general_category, GeneralCategory as Category};

pub type Config = BTreeMap<String, String>;
pub const MAX_CONFIG: usize = 65536;

pub fn defaults() -> Config {
    [
        ("WIFI_IFACE", "wlan0"),
        ("INTERNET_IFACE", "wlan0"),
        ("SSID", "Hotspot"),
        // Compatibility only: every new file must override this historical default.
        ("PASSPHRASE", "12345678"),
        ("FREQ_BAND", "auto"),
        ("CHANNEL", "default"),
        ("GATEWAY", "192.168.12.1"),
        ("WPA_VERSION", "2"),
        ("ETC_HOSTS", "0"),
        ("DHCP_DNS", "gateway"),
        ("NO_DNS", "0"),
        ("NO_DNSMASQ", "0"),
        ("HIDDEN", "0"),
        ("MAC_FILTER", "0"),
        ("MAC_FILTER_ACCEPT", "/etc/hostapd/hostapd.accept"),
        ("ISOLATE_CLIENTS", "0"),
        ("SHARE_METHOD", "nat"),
        ("IEEE80211N", "0"),
        ("IEEE80211AC", "0"),
        ("IEEE80211AX", "0"),
        ("NO_VIRT", "0"),
        ("USE_PSK", "0"),
        ("BACKEND", "create_ap"),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v.into()))
    .collect()
}

/// Match Python's Unicode-16 str.isprintable, including its ASCII-space exception.
pub fn printable(c: char) -> bool {
    c == ' '
        || !matches!(
            get_general_category(c),
            Category::Control
                | Category::Format
                | Category::PrivateUse
                | Category::Surrogate
                | Category::Unassigned
                | Category::SpaceSeparator
                | Category::LineSeparator
                | Category::ParagraphSeparator
        )
}

pub fn validate(config: &Config) -> Result<Config, &'static str> {
    let mut result = defaults();
    result.extend(config.clone());
    for (key, value) in &result {
        validate_field(key, value)?;
    }
    let password = &result["PASSPHRASE"];
    if result["USE_PSK"] == "1" {
        if password.len() != 64 || !password.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("PASSPHRASE must be a 64-digit hexadecimal PSK");
        }
    } else if !password.is_ascii() || !(8..=63).contains(&password.len()) {
        return Err("PASSPHRASE must be 8–63 printable ASCII characters");
    }
    Ok(result)
}

pub fn merge(current: &Config, patch: &Config) -> Result<Config, &'static str> {
    let mut merged = current.clone();
    merged.extend(patch.clone());
    validate(&merged)
}

/// Existing files use literal KEY=value records, not shell evaluation or quoting.
/// Duplicate assignments retain their last value, matching the old reader.
pub fn parse_file(input: &str) -> Result<Config, &'static str> {
    validate(&parse_records(input)?)
}

/// Raw literal records let credential storage distinguish missing secrets from
/// historical schema defaults. Production files must carry an explicit password.
pub fn parse_records(input: &str) -> Result<Config, &'static str> {
    if input.len() > MAX_CONFIG {
        return Err("Configuration exceeds 64 KiB");
    }
    let mut config = Config::new();
    for line in input.split('\n') {
        let line = line.trim_end_matches('\r');
        if line.trim_start().starts_with('#') {
            continue;
        }
        if let Some((key, value)) = line.split_once('=') {
            config.insert(key.trim().into(), value.into());
        }
    }
    Ok(config)
}

pub fn serialize_file(config: &Config) -> Result<String, &'static str> {
    let config = validate(config)?;
    let mut text = String::from("# Wi-Fi Hotspot Configuration\n");
    for (key, value) in config {
        text.push_str(&format!("{key}={value}\n"));
    }
    if text.len() > MAX_CONFIG {
        return Err("Configuration exceeds 64 KiB");
    }
    Ok(text)
}

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
    if v.chars().count() > 4096 || v.is_empty() || !v.chars().all(printable) {
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

    #[test]
    fn validates_psk_against_the_merged_use_psk_flag() {
        let mut patch = Config::from([("USE_PSK".into(), "1".into())]);
        assert!(merge(&defaults(), &patch).is_err());
        patch.insert("PASSPHRASE".into(), "a".repeat(64));
        let current = merge(&defaults(), &patch).unwrap();
        assert!(merge(
            &current,
            &Config::from([("SSID".into(), "New name".into())])
        )
        .is_ok());
        assert!(merge(&current, &Config::from([("USE_PSK".into(), "0".into())])).is_err());
    }

    #[test]
    fn configuration_file_round_trip_preserves_literal_password() {
        let config = merge(
            &defaults(),
            &Config::from([("PASSPHRASE".into(), "  literal=#'\\$()  ".into())]),
        )
        .unwrap();
        assert_eq!(
            parse_file(&serialize_file(&config).unwrap()).unwrap(),
            config
        );
        let parsed =
            parse_file("# comment\r\nSSID=Old\nSSID=New\r\nPASSPHRASE=password=literal\n").unwrap();
        assert_eq!(parsed["SSID"], "New");
        assert_eq!(parsed["PASSPHRASE"], "password=literal");
    }

    #[test]
    fn rejects_invisible_unicode_and_private_use_values() {
        for c in [
            '\u{a0}', '\u{ad}', '\u{200b}', '\u{2028}', '\u{e000}', '\u{378}',
        ] {
            assert!(!printable(c));
            assert!(
                parse_patch(&serde_json::json!({"SSID":format!("ab{c}")}).to_string()).is_err()
            );
        }
        assert!(printable(' '));
        assert!(printable('测'));
    }
}
