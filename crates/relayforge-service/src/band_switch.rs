//! Native upstream band switching. No nmcli, no shell and no credential in argv.
//!
//! The desktop controls offer `SwitchBandAndReconnect` because a hotspot may only use
//! channels the PHY advertises without DFS/CAC. When the upstream station is associated
//! on a channel that is not permitted, the station itself has to move to a BSSID on a
//! permitted channel of the wanted band. This module performs that reconnect over the
//! NetworkManager D-Bus API and verifies the result before reporting success.
//!
//! The saved upstream profile is never modified. If NetworkManager accepts the BSSID
//! but immediately roams back, one activation is retried with a *volatile* copy of the
//! profile whose band is constrained, leaving the administrator's stored profile intact.
use crate::{
    networkmanager::{string, Bus, Platform},
    nm_journal::Result,
};
use relayforge_core::{config::Config, radio::Snapshot};
use serde_json::{json, Value};
use std::{collections::HashMap, time::Duration};
use zbus::zvariant::{OwnedObjectPath, OwnedValue};

const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const SETTINGS: &str = "/org/freedesktop/NetworkManager/Settings";
const SETTINGS_INTERFACE: &str = "org.freedesktop.NetworkManager.Settings";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const ACCESS_POINT: &str = "org.freedesktop.NetworkManager.AccessPoint";
const ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
const SCAN_TIMEOUT: Duration = Duration::from_secs(4);
const ACTIVATION_TIMEOUT: Duration = Duration::from_secs(25);
const POLL: Duration = Duration::from_millis(250);

struct Candidate {
    path: OwnedObjectPath,
    strength: u8,
}

/// Read one access point and keep it only when it serves the wanted network on a
/// channel this adapter may use for an AP.
async fn candidate(
    bus: &Bus,
    path: &OwnedObjectPath,
    ssid: &[u8],
    allowed: &[u32],
) -> Result<Option<Candidate>> {
    let Ok(advertised) = bus
        .property::<Vec<u8>>(path.as_str(), ACCESS_POINT, "Ssid")
        .await
    else {
        return Ok(None);
    };
    if advertised != ssid {
        return Ok(None);
    }
    let Ok(frequency) = bus
        .property::<u32>(path.as_str(), ACCESS_POINT, "Frequency")
        .await
    else {
        return Ok(None);
    };
    if !allowed.contains(&frequency) {
        return Ok(None);
    }
    let Ok(strength) = bus
        .property::<u8>(path.as_str(), ACCESS_POINT, "Strength")
        .await
    else {
        return Ok(None);
    };
    Ok(Some(Candidate {
        path: path.clone(),
        strength,
    }))
}

/// Request a fresh scan, then pick the strongest permitted BSSID for this network.
async fn scan(
    bus: &Bus,
    device: &OwnedObjectPath,
    ssid: &[u8],
    allowed: &[u32],
) -> Result<Vec<Candidate>> {
    // A scan that is already running is refused; stale results are still usable.
    let _: std::result::Result<(), _> = bus
        .call(
            device.as_str(),
            WIRELESS,
            "RequestScan",
            &(HashMap::<String, OwnedValue>::new(),),
        )
        .await;
    let deadline = tokio::time::Instant::now() + SCAN_TIMEOUT;
    loop {
        let points = bus
            .property::<Vec<OwnedObjectPath>>(device.as_str(), WIRELESS, "AccessPoints")
            .await?;
        let mut found = Vec::new();
        for path in points.iter().take(4096) {
            if let Some(candidate) = candidate(bus, path, ssid, allowed).await? {
                found.push(candidate);
            }
        }
        if !found.is_empty() {
            return Ok(found);
        }
        if tokio::time::Instant::now() >= deadline {
            return Ok(Vec::new());
        }
        tokio::time::sleep(POLL).await;
    }
}

/// The saved profile currently carrying the upstream connection.
async fn profile(bus: &Bus, device: &OwnedObjectPath) -> Result<OwnedObjectPath> {
    let active: OwnedObjectPath = bus.property(device.as_str(), DEVICE, "ActiveConnection").await?;
    if active.as_str() == "/" {
        return Err("Cannot identify the active Wi-Fi profile");
    }
    let uuid: String = bus.property(active.as_str(), ACTIVE, "Uuid").await?;
    bus.call(SETTINGS, SETTINGS_INTERFACE, "GetConnectionByUuid", &(uuid,)).await
    .map_err(|_| "Cannot locate the active Wi-Fi profile")
}

async fn wait_active(bus: &Bus, active: &OwnedObjectPath) -> Result<()> {
    let deadline = tokio::time::Instant::now() + ACTIVATION_TIMEOUT;
    loop {
        let state: u32 = bus.property(active.as_str(), ACTIVE, "State").await?;
        if state == 2 {
            return Ok(());
        }
        if state >= 3 || tokio::time::Instant::now() >= deadline {
            return Err("Wi-Fi did not reconnect on a permitted hotspot channel");
        }
        tokio::time::sleep(POLL).await;
    }
}

/// Activate `specific` (a BSSID, or `/` for any) against `profile`.
async fn activate(
    bus: &Bus,
    profile: &OwnedObjectPath,
    device: &OwnedObjectPath,
    specific: &OwnedObjectPath,
) -> Result<()> {
    let (active,): (OwnedObjectPath,) = bus
        .call(
            ROOT,
            NM,
            "ActivateConnection",
            &(profile.clone(), device.clone(), specific.clone()),
        )
        .await?;
    wait_active(&bus, &active).await
}

fn verified(snapshot: &Snapshot, target: &str, ssid: &str) -> bool {
    let caps = &snapshot.capabilities;
    caps.current_sta_band == target
        && caps.current_sta_ssid == ssid
        && caps.current_sta_ap_allowed
        && caps.ap_sta_concurrent
}

/// Constrain a copy of the profile to one band. The stored profile is never edited, so
/// a failed or interrupted switch cannot leave an administrator's saved setting changed.
/// `OwnedValue` is not `Clone`, so the profile has to be copied value by value.
fn copy(settings: &crate::networkmanager::Settings) -> Result<crate::networkmanager::Settings> {
    let mut result = crate::networkmanager::Settings::new();
    for (section, fields) in settings {
        let mut copied = HashMap::new();
        for (key, value) in fields {
            copied.insert(
                key.clone(),
                value
                    .try_clone()
                    .map_err(|_| "Cannot copy the upstream Wi-Fi profile")?,
            );
        }
        result.insert(section.clone(), copied);
    }
    Ok(result)
}
fn pin_band(
    settings: &crate::networkmanager::Settings,
    target: &str,
) -> Result<crate::networkmanager::Settings> {
    let mut result = copy(settings)?;
    let wireless = result
        .get_mut("802-11-wireless")
        .ok_or("The upstream profile has no wireless settings")?;
    wireless.insert(
        "band".into(),
        string(if target == "2.4" { "bg" } else { "a" }),
    );
    // A fresh volatile profile must not collide with the saved one.
    if let Some(connection) = result.get_mut("connection") {
        connection.remove("uuid");
        connection.insert("id".into(), string("RelayForge band switch"));
    }
    Ok(result)
}

async fn run(fixture: bool, config: &Config, target: &str) -> Result<Value> {
    if !["2.4", "5"].contains(&target) {
        return Err("Unsupported Wi-Fi band");
    }
    let bus = Bus::open(fixture).await?;
    let result = async {
        let platform = Platform { fixture };
        let iface = &config["WIFI_IFACE"];
        let snapshot = platform.inspect(&bus, iface).await?;
        let ssid = snapshot.capabilities.current_sta_ssid.clone();
        if ssid.is_empty() {
            return Err("No active Wi-Fi connection to switch");
        }
        let allowed: Vec<u32> = snapshot
            .capabilities
            .ap_channels
            .get(target)
            .map(|channels| channels.iter().map(|c| c.frequency).collect())
            .unwrap_or_default();
        if allowed.is_empty() {
            return Err("No permitted hotspot channel is available in this band");
        }
        let device = bus.device(iface).await?;
        let best = scan(&bus, &device, ssid.as_bytes(), &allowed)
            .await?
            .into_iter()
            .max_by_key(|candidate| candidate.strength)
            .ok_or("No reachable access point for this network on a permitted hotspot channel")?;
        let profile = profile(&bus, &device).await?;
        activate(&bus, &profile, &device, &best.path).await?;
        let observed = platform.inspect(&bus, iface).await?;
        if verified(&observed, target, &ssid) {
            return Ok(json!({"success":true,"target_band":target,"ssid":ssid}));
        }
        // Some NetworkManager/driver combinations accept the BSSID and then select a
        // stronger AP on the old band. Constrain one volatile activation instead.
        let settings = bus.settings(profile.as_str()).await?;
        let pinned = pin_band(&settings, target)?;
        let options = HashMap::from([("persist".to_owned(), string("volatile"))]);
        let (_, active, _): (
            OwnedObjectPath,
            OwnedObjectPath,
            HashMap<String, OwnedValue>,
        ) = bus
            .call(
                ROOT,
                NM,
                "AddAndActivateConnection2",
                &(pinned, device.clone(), best.path.clone(), options),
            )
            .await?;
        wait_active(&bus, &active).await?;
        let observed = platform.inspect(&bus, iface).await?;
        if verified(&observed, target, &ssid) {
            return Ok(json!({"success":true,"target_band":target,"ssid":ssid}));
        }
        // Best effort: return to the saved profile rather than leaving the station
        // associated with a volatile one.
        let _ = activate(
            &bus,
            &profile,
            &device,
            &OwnedObjectPath::try_from("/").unwrap(),
        )
        .await;
        Err("Wi-Fi did not reconnect on a permitted hotspot channel")
    }
    .await;
    bus.close().await;
    result
}

/// Never fails loudly: the reply contract is a JSON result, matching the pinned engine.
pub(crate) async fn switch(fixture: bool, config: &Config, target: &str) -> Value {
    match run(fixture, config, target).await {
        Ok(reply) => reply,
        Err(error) => json!({"success":false,"error":error}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use relayforge_core::config::defaults;
    #[tokio::test]
    async fn unsupported_bands_and_absent_radio_fail_closed_without_a_bus() {
        let config = defaults();
        assert_eq!(
            switch(true, &config, "6").await["success"],
            Value::Bool(false)
        );
    }
}
