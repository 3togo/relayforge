//! Conservative iw decoding and Start policy. No processes, bus calls or I/O.
use crate::config::{self, Config};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const MAX_CAPTURE: usize = 1024 * 1024;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Capture {
    pub wifi_iface: String,
    pub info: String,
    pub phy: String,
    pub link: String,
}

impl Capture {
    pub fn demo(iface: &str) -> Self {
        Self {wifi_iface:iface.into(),info:format!("Interface {iface}\n\ttype managed\n\twiphy 0\n"),
            phy:"Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412 MHz [1] (20.0 dBm)\n\tvalid interface combinations:\n\t\t * #{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1\n".into(),
            link:"Not connected.\n".into()}
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Channel {
    pub frequency: u32,
    pub channel: u16,
}

#[derive(Clone, Serialize)]
pub struct Capabilities {
    pub ap_2ghz: bool,
    pub ap_5ghz: bool,
    pub ap_channels: BTreeMap<String, Vec<Channel>>,
    pub current_sta_band: String,
    pub current_sta_frequency: u32,
    pub current_sta_ssid: String,
    pub current_sta_bssid: String,
    pub current_sta_ap_allowed: bool,
    pub ap_sta_concurrent: bool,
    pub wifi_iface: String,
}

pub struct Snapshot {
    pub phy: u32,
    pub capabilities: Capabilities,
    pub interface_type: String,
}

#[derive(Debug, Serialize)]
pub struct StartPlan {
    pub band: String,
    pub channel: Option<u16>,
    pub switch_upstream: bool,
}

pub fn band(frequency: u32) -> &'static str {
    match frequency {
        2400..2500 => "2.4",
        4900..5925 => "5",
        _ => "",
    }
}

fn bounded(text: &str) -> Result<(), &'static str> {
    if text.len() > MAX_CAPTURE || text.contains('\0') {
        Err("Invalid or oversized radio inspection output")
    } else {
        Ok(())
    }
}

pub fn device(info: &str, iface: &str) -> Result<(u32, String), &'static str> {
    bounded(info)?;
    config::parse_patch(&serde_json::json!({"WIFI_IFACE":iface}).to_string())?;
    let mut lines = info.lines().filter(|line| !line.trim().is_empty());
    if lines.next().map(str::trim) != Some(format!("Interface {iface}").as_str()) {
        return Err("Radio interface identity does not match the requested adapter");
    }
    let mut phy = None;
    let mut kind = None;
    for line in lines {
        let line = line.trim();
        if line.starts_with("Interface ") {
            return Err("Ambiguous radio interface identity");
        }
        if let Some(value) = line.strip_prefix("wiphy ") {
            if phy.is_some() {
                return Err("Ambiguous radio PHY identity");
            }
            phy = Some(
                value
                    .parse::<u32>()
                    .map_err(|_| "Invalid radio PHY identity")?,
            );
        }
        if let Some(value) = line.strip_prefix("type ") {
            if kind.is_some() || value.is_empty() {
                return Err("Invalid radio interface type");
            }
            kind = Some(value.into());
        }
    }
    Ok((
        phy.ok_or("Missing radio PHY identity")?,
        kind.ok_or("Missing radio interface type")?,
    ))
}

fn indent(line: &str) -> usize {
    line.len() - line.trim_start().len()
}

fn section<'a>(text: &'a str, heading: &str) -> Vec<&'a str> {
    let mut result = Vec::new();
    let mut level = None;
    for line in text.lines() {
        if let Some(level) = level {
            if line.trim().is_empty() {
                continue;
            }
            if indent(line) <= level {
                break;
            }
            result.push(line);
        } else if line.trim() == heading {
            level = Some(indent(line));
        }
    }
    result
}

fn number(input: &mut &str) -> Option<u32> {
    *input = input.trim_start();
    let length = input.bytes().take_while(u8::is_ascii_digit).count();
    if length == 0 {
        return None;
    }
    let value = input[..length].parse().ok()?;
    *input = &input[length..];
    Some(value)
}

fn prefix(input: &mut &str, value: &str) -> bool {
    if let Some(rest) = input.trim_start().strip_prefix(value) {
        *input = rest;
        true
    } else {
        false
    }
}

fn combination_allows_pair(mut text: &str) -> bool {
    let mut managed = false;
    let mut ap = false;
    let mut groups = 0;
    while prefix(&mut text, "#{") {
        let Some((types, rest)) = text.split_once('}') else {
            return false;
        };
        let types: Vec<_> = types.split(',').map(str::trim).collect();
        if types.iter().any(|t| t.is_empty()) {
            return false;
        }
        text = rest;
        if !prefix(&mut text, "<=") {
            return false;
        }
        let Some(limit) = number(&mut text) else {
            return false;
        };
        let has_managed = types.contains(&"managed");
        let has_ap = types.contains(&"AP");
        if u32::from(has_managed) + u32::from(has_ap) > limit {
            return false;
        }
        managed |= has_managed;
        ap |= has_ap;
        groups += 1;
        if !prefix(&mut text, ",") {
            return false;
        }
    }
    if !prefix(&mut text, "total <=") {
        return false;
    }
    let total = number(&mut text).unwrap_or(0);
    if !prefix(&mut text, ",") || !prefix(&mut text, "#channels <=") {
        return false;
    }
    let channels = number(&mut text).unwrap_or(0);
    // Unknown extra constraints are not treated as permission to activate.
    groups > 0 && managed && ap && total >= 2 && channels >= 1 && text.trim().is_empty()
}

fn allowed_power_tail(tail: &str) -> bool {
    let tail = tail.trim();
    if tail.is_empty() {
        return true;
    }
    let Some(power) = tail.strip_prefix('(').and_then(|v| v.strip_suffix(" dBm)")) else {
        return false;
    };
    power.parse::<f64>().is_ok_and(f64::is_finite)
}

fn channel_line(line: &str) -> Option<(Channel, bool)> {
    let mut line = line.trim().strip_prefix("* ")?;
    let (frequency, rest) = line.split_once(" MHz [")?;
    let frequency = frequency.parse::<f64>().ok()?;
    if !frequency.is_finite() || frequency.fract() != 0.0 || !(1.0..=100_000.0).contains(&frequency)
    {
        return None;
    }
    line = rest;
    let (channel, tail) = line.split_once(']')?;
    let channel = channel.parse::<u16>().ok()?;
    let frequency = frequency as u32;
    let consistent = match band(frequency) {
        "2.4" => {
            (1..=13).contains(&channel) && frequency == 2407 + 5 * u32::from(channel)
                || channel == 14 && frequency == 2484
        }
        "5" => {
            (1..=196).contains(&channel)
                && (frequency == 5000 + 5 * u32::from(channel)
                    || channel >= 180 && frequency == 4000 + 5 * u32::from(channel))
        }
        _ => false,
    };
    Some((
        Channel { frequency, channel },
        consistent && allowed_power_tail(tail),
    ))
}

pub fn phy_channels(
    text: &str,
    expected_phy: u32,
) -> Result<(BTreeMap<String, Vec<Channel>>, bool), &'static str> {
    bounded(text)?;
    if text
        .lines()
        .find(|line| !line.trim().is_empty())
        .map(str::trim)
        != Some(format!("Wiphy phy{expected_phy}").as_str())
    {
        return Err("Radio PHY identity changed during inspection");
    }
    if text
        .lines()
        .filter(|line| line.trim().starts_with("Wiphy "))
        .count()
        != 1
    {
        return Err("Ambiguous radio PHY output");
    }
    let ap = section(text, "Supported interface modes:")
        .iter()
        .any(|line| line.trim() == "* AP");
    let mut observations = BTreeMap::new();
    let mut frequencies = None;
    for line in text.lines() {
        if line.trim() == "Frequencies:" {
            frequencies = Some(indent(line));
            continue;
        }
        if let Some(level) = frequencies {
            if !line.trim().is_empty() && indent(line) <= level {
                frequencies = None;
                continue;
            }
            if let Some((channel, allowed)) = channel_line(line) {
                observations
                    .entry((channel.frequency, channel.channel))
                    .and_modify(|value| *value &= allowed)
                    .or_insert(allowed);
            }
        }
    }
    let mut channels = BTreeMap::from([("2.4".into(), Vec::new()), ("5".into(), Vec::new())]);
    if ap {
        for ((frequency, channel), allowed) in observations {
            if allowed {
                channels
                    .get_mut(band(frequency))
                    .unwrap()
                    .push(Channel { frequency, channel });
            }
        }
    }
    let mut combinations = Vec::new();
    let mut current = String::new();
    for line in section(text, "valid interface combinations:") {
        if let Some(part) = line.trim().strip_prefix("* ") {
            if !current.is_empty() {
                combinations.push(std::mem::take(&mut current));
            }
            current.push_str(part);
        } else {
            current.push(' ');
            current.push_str(line.trim());
        }
    }
    if !current.is_empty() {
        combinations.push(current);
    }
    let concurrent = ap
        && combinations
            .iter()
            .any(|text| combination_allows_pair(text));
    Ok((channels, concurrent))
}

impl Snapshot {
    pub fn parse(record: &Capture) -> Result<Self, &'static str> {
        let (phy, kind) = device(&record.info, &record.wifi_iface)?;
        let (channels, concurrent) = phy_channels(&record.phy, phy)?;
        bounded(&record.link)?;
        let connected: Vec<_> = record
            .link
            .lines()
            .map(str::trim)
            .filter(|line| line.starts_with("Connected to "))
            .collect();
        let mut ssid = String::new();
        let mut frequency = 0;
        let mut bssid = String::new();
        if connected.is_empty() {
            if record.link.trim() != "Not connected." {
                return Err("Unrecognized station link state");
            }
        } else {
            if connected.len() != 1 || record.link.contains("Not connected.") {
                return Err("Ambiguous station link state");
            }
            bssid = connected[0]
                .split_whitespace()
                .nth(2)
                .ok_or("Missing station BSSID")?
                .into();
            let bytes: Vec<_> = bssid.split(':').collect();
            if bytes.len() != 6
                || !bytes
                    .iter()
                    .all(|v| v.len() == 2 && v.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err("Invalid station BSSID");
            }
            let mut ssids = 0;
            let mut frequencies = 0;
            for line in record.link.lines().map(str::trim) {
                if let Some(value) = line.strip_prefix("SSID:") {
                    ssid = value.trim().into();
                    ssids += 1;
                }
                if let Some(value) = line.strip_prefix("freq:") {
                    let number = value
                        .trim()
                        .parse::<f64>()
                        .map_err(|_| "Invalid station frequency")?;
                    if !number.is_finite()
                        || number.fract() != 0.0
                        || !(1.0..=100_000.0).contains(&number)
                    {
                        return Err("Invalid station frequency");
                    }
                    frequency = number as u32;
                    frequencies += 1;
                }
            }
            if ssids != 1 || frequencies != 1 || ssid.is_empty() {
                return Err("Incomplete station link inspection");
            }
        }
        let band = band(frequency);
        let permitted = channels
            .get(band)
            .is_some_and(|entries| entries.iter().any(|c| c.frequency == frequency));
        Ok(Self {
            phy,
            interface_type: kind,
            capabilities: Capabilities {
                ap_2ghz: !channels["2.4"].is_empty(),
                ap_5ghz: !channels["5"].is_empty(),
                ap_channels: channels,
                current_sta_band: band.into(),
                current_sta_frequency: frequency,
                current_sta_ssid: ssid,
                current_sta_bssid: bssid,
                current_sta_ap_allowed: permitted,
                ap_sta_concurrent: concurrent,
                wifi_iface: record.wifi_iface.clone(),
            },
        })
    }

    /// Native configuration/radio policy only; it does not prove daemon or
    /// network readiness. Compatibility mode retains its separate switch policy.
    pub fn native_preflight(&self, config: &Config) -> Result<StartPlan, &'static str> {
        self.native_preflight_with_bridge(config, false)
    }

    pub fn native_preflight_with_bridge(
        &self,
        config: &Config,
        allow_bridge: bool,
    ) -> Result<StartPlan, &'static str> {
        let config = config::validate(config)?;
        let plan = self.preflight(&config)?;
        if config["BACKEND"] == "create_ap" {
            crate::ap_policy::security(&config)?;
            if config["SHARE_METHOD"] == "bridge" {
                if !allow_bridge {
                    return Err("Native create_ap declines bridge mode because it reconfigures the upstream interface");
                }
                if config["NO_VIRT"] != "0" || config["WIFI_IFACE"] == config["INTERNET_IFACE"] {
                    return Err("Existing-bridge sharing requires a separate administrator bridge and an owned virtual AP");
                }
            }
            if plan.switch_upstream {
                return Err(
                    "Native create_ap preserves the upstream; reconnect explicitly before Start",
                );
            }
        }
        Ok(plan)
    }

    pub fn preflight(&self, config: &Config) -> Result<StartPlan, &'static str> {
        let config = config::validate(config)?;
        let caps = &self.capabilities;
        if config["WIFI_IFACE"] != caps.wifi_iface {
            return Err("Configured adapter changed during radio inspection");
        }
        let connected = !caps.current_sta_ssid.is_empty();
        let requested = config["FREQ_BAND"].as_str();
        if config["BACKEND"] == "networkmanager" {
            if [
                "NO_VIRT",
                "NO_DNS",
                "NO_DNSMASQ",
                "ETC_HOSTS",
                "MAC_FILTER",
                "IEEE80211N",
                "IEEE80211AC",
                "IEEE80211AX",
            ]
            .iter()
            .any(|key| config[*key] != "0")
                || config["WPA_VERSION"] != "2"
                || config["SHARE_METHOD"] != "nat"
                || config["DHCP_DNS"] != "gateway"
                || config.contains_key("COUNTRY")
            {
                return Err("NetworkManager backend does not support the configured overrides");
            }
            if self.interface_type != "managed" || !connected {
                return Err("Connect the selected managed Wi-Fi adapter before sharing");
            }
            if !caps.ap_sta_concurrent || !caps.current_sta_ap_allowed {
                return Err("Concurrent sharing is not permitted on the current upstream channel");
            }
            if requested != "auto" && requested != caps.current_sta_band {
                return Err("Requested band does not match the upstream Wi-Fi connection");
            }
            let channel = caps.ap_channels[&caps.current_sta_band]
                .iter()
                .find(|c| c.frequency == caps.current_sta_frequency)
                .unwrap()
                .channel;
            if config["CHANNEL"] != "default"
                && config["CHANNEL"].parse::<u16>().ok() != Some(channel)
            {
                return Err("Concurrent sharing must use the upstream Wi-Fi channel");
            }
            return Ok(StartPlan {
                band: caps.current_sta_band.clone(),
                channel: Some(channel),
                switch_upstream: false,
            });
        }
        if connected {
            if !caps.ap_sta_concurrent || config["NO_VIRT"] == "1" {
                return Err("This adapter/configuration cannot run Wi-Fi and a hotspot together; use a second adapter");
            }
            let target = if requested != "auto" {
                requested
            } else if caps.current_sta_ap_allowed {
                &caps.current_sta_band
            } else if caps.ap_2ghz {
                "2.4"
            } else {
                "5"
            };
            if caps.ap_channels[target].is_empty() {
                return Err("No permitted hotspot channel is available in the requested band");
            }
            let switch = !caps.current_sta_ap_allowed || caps.current_sta_band != target;
            let channel = if switch {
                None
            } else {
                caps.ap_channels[target]
                    .iter()
                    .find(|c| c.frequency == caps.current_sta_frequency)
                    .map(|c| c.channel)
            };
            return Ok(StartPlan {
                band: target.into(),
                channel,
                switch_upstream: switch,
            });
        }
        if config["WIFI_IFACE"] == config["INTERNET_IFACE"] {
            return Err("Connect to Wi-Fi before sharing this adapter's connection");
        }
        let target = if requested != "auto" {
            requested
        } else if caps.ap_5ghz {
            "5"
        } else {
            "2.4"
        };
        let channels = &caps.ap_channels[target];
        if channels.is_empty() {
            return Err("No permitted hotspot channel is available in the requested band");
        }
        let channel = if config["CHANNEL"] == "default" {
            channels[0].channel
        } else {
            let wanted = config["CHANNEL"].parse::<u16>().unwrap();
            if !channels.iter().any(|c| c.channel == wanted) {
                return Err("The configured hotspot channel is not permitted");
            }
            wanted
        };
        Ok(StartPlan {
            band: target.into(),
            channel: Some(channel),
            switch_upstream: false,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn adapter_without_ap_mode_cannot_be_used_as_hotspot() {
        let mut record = Capture::demo("wlxe84e0648d7eb");
        record.phy = record.phy.replace("* AP", "* monitor");
        let radio = Snapshot::parse(&record).unwrap();
        assert!(!radio.capabilities.ap_2ghz && !radio.capabilities.ap_5ghz);
        let mut config = config::defaults();
        config.insert("WIFI_IFACE".into(), "wlxe84e0648d7eb".into());
        config.insert("INTERNET_IFACE".into(), "eth0".into());
        for backend in ["create_ap", "networkmanager"] {
            config.insert("BACKEND".into(), backend.into());
            assert!(radio.preflight(&config).is_err());
        }
    }
    #[test]
    fn unsupported_flags_are_never_permissions_and_duplicates_intersect() {
        for flag in [
            "disabled",
            "no IR",
            "no-ir",
            "passive scanning",
            "radar detection",
            "DFS",
            "indoor only",
            "no initiate radiation",
            "unknown restriction",
        ] {
            let mut record = Capture::demo("demo0");
            record.phy = record
                .phy
                .replace("(20.0 dBm)", &format!("(20.0 dBm) ({flag})"));
            assert!(
                !Snapshot::parse(&record).unwrap().capabilities.ap_2ghz,
                "{flag}"
            );
        }
        let mut record = Capture::demo("demo0");
        record.phy = record
            .phy
            .replace("(20.0 dBm)", "(20.0 dBm)\n\t\t\t* 2412 MHz [1] (disabled)");
        assert!(!Snapshot::parse(&record).unwrap().capabilities.ap_2ghz);
    }
    #[test]
    fn concurrency_requires_two_real_slots_and_known_constraints() {
        for (combination, expected) in [
            ("#{ managed, AP } <= 1, total <= 2, #channels <= 1", false),
            ("#{ managed, AP } <= 2, total <= 2, #channels <= 1", true),
            (
                "#{ managed } <= 0, #{ AP } <= 1, total <= 2, #channels <= 1",
                false,
            ),
            (
                "#{ managed } <= 1, #{ AP } <= 1, total <= 1, #channels <= 1",
                false,
            ),
            (
                "#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 0",
                false,
            ),
            (
                "#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1, unknown constraint",
                false,
            ),
        ] {
            assert_eq!(combination_allows_pair(combination), expected);
        }
    }
    #[test]
    fn malformed_or_mismatched_identity_and_link_are_rejected() {
        let record = Capture::demo("demo0");
        let mut other = record.clone();
        other.phy = other.phy.replace("Wiphy phy0", "Wiphy phy1");
        assert!(Snapshot::parse(&other).is_err());
        other = record.clone();
        other.info = other.info.replace("Interface demo0", "Interface other0");
        assert!(Snapshot::parse(&other).is_err());
        other = record.clone();
        other.link = "Connected to aa:bb:cc:dd:ee:ff\n".into();
        assert!(Snapshot::parse(&other).is_err());
        other = record;
        other.phy.push('\0');
        assert!(Snapshot::parse(&other).is_err());
    }
    #[test]
    fn preflight_plans_dedicated_ap_without_mutation_and_rejects_self_uplink() {
        let snapshot = Snapshot::parse(&Capture::demo("demo0")).unwrap();
        let mut config = config::merge(
            &config::defaults(),
            &Config::from([
                ("WIFI_IFACE".into(), "demo0".into()),
                ("INTERNET_IFACE".into(), "eth0".into()),
            ]),
        )
        .unwrap();
        assert_eq!(snapshot.preflight(&config).unwrap().channel, Some(1));
        config.insert("CHANNEL".into(), "6".into());
        assert!(snapshot.preflight(&config).is_err());
        config.insert("CHANNEL".into(), "default".into());
        config.insert("INTERNET_IFACE".into(), "demo0".into());
        assert!(snapshot.preflight(&config).is_err());
    }

    #[test]
    fn native_policy_explains_unsupported_options_without_changing_compatibility_plans() {
        let snapshot = Snapshot::parse(&Capture::demo("demo0")).unwrap();
        let mut config = config::merge(
            &config::defaults(),
            &Config::from([
                ("WIFI_IFACE".into(), "demo0".into()),
                ("INTERNET_IFACE".into(), "eth0".into()),
            ]),
        )
        .unwrap();
        assert!(snapshot.native_preflight(&config).is_ok());
        config.insert("SHARE_METHOD".into(), "bridge".into());
        assert!(snapshot.preflight(&config).is_ok());
        assert!(snapshot
            .native_preflight(&config)
            .unwrap_err()
            .contains("bridge mode"));
        config.insert("SHARE_METHOD".into(), "nat".into());
        config.insert("COUNTRY".into(), "CN".into());
        // COUNTRY is a read-only requirement, verified by the service/runtime.
        assert!(snapshot.native_preflight(&config).is_ok());
    }
    #[test]
    fn nm_requires_current_permitted_channel_and_supported_options() {
        let mut record = Capture::demo("demo0");
        record.link =
            "Connected to aa:bb:cc:dd:ee:ff (on demo0)\n\tSSID: Test:Network\n\tfreq: 2412.0\n"
                .into();
        let snapshot = Snapshot::parse(&record).unwrap();
        let mut config = config::merge(
            &config::defaults(),
            &Config::from([
                ("WIFI_IFACE".into(), "demo0".into()),
                ("BACKEND".into(), "networkmanager".into()),
            ]),
        )
        .unwrap();
        assert!(!snapshot.preflight(&config).unwrap().switch_upstream);
        config.insert("CHANNEL".into(), "6".into());
        assert!(snapshot.preflight(&config).is_err());
        config.insert("CHANNEL".into(), "default".into());
        config.insert("NO_DNS".into(), "1".into());
        assert!(snapshot.preflight(&config).is_err());
    }
}
