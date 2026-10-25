//! Read-only Linux sysfs inventory; no subprocesses and no invented devices.
use serde::Serialize;
use std::path::Path;

#[derive(Serialize)]
pub struct Interfaces {
    pub wifi_interfaces: Vec<String>,
    pub all_interfaces: Vec<String>,
}

/// Choose an adapter only when a fresh install has one unambiguous Wi-Fi device.
pub fn unique_wifi(root: &Path) -> Option<String> {
    let found = discover(root).ok()?;
    match found.wifi_interfaces.as_slice() {
        [only] => Some(only.clone()),
        _ => None,
    }
}

pub fn discover(root: &Path) -> Result<Interfaces, &'static str> {
    let mut wifi = Vec::new();
    let mut all = Vec::new();
    for entry in
        std::fs::read_dir(root).map_err(|_| "Cannot read Linux network interface inventory")?
    {
        let entry = entry.map_err(|_| "Cannot read Linux network interface entry")?;
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if name == "lo" {
            continue;
        }
        if !entry.path().exists() {
            continue;
        }
        if entry.path().join("wireless").exists() || entry.path().join("phy80211").exists() {
            wifi.push(name.clone());
        }
        all.push(name);
    }
    all.sort();
    wifi.sort();
    Ok(Interfaces {
        wifi_interfaces: wifi,
        all_interfaces: all,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifies_wireless_markers_and_returns_empty_lists_without_adapters() {
        let root = std::env::temp_dir().join(format!(
            "relayforge-inventory-{}",
            super::super::config_store::random_secret().unwrap()
        ));
        std::fs::create_dir(&root).unwrap();
        assert!(discover(&root).unwrap().all_interfaces.is_empty());
        for name in ["lo", "eth0", "wlp2s0", "wrnm012345"] {
            std::fs::create_dir(root.join(name)).unwrap();
        }
        std::fs::create_dir(root.join("wlp2s0/wireless")).unwrap();
        std::os::unix::fs::symlink(
            root.join("wlp2s0/wireless"),
            root.join("wrnm012345/phy80211"),
        )
        .unwrap();
        let found = discover(&root).unwrap();
        assert_eq!(found.wifi_interfaces, ["wlp2s0", "wrnm012345"]);
        assert_eq!(found.all_interfaces, ["eth0", "wlp2s0", "wrnm012345"]);
        // Detection is only unambiguous with exactly one Wi-Fi adapter.
        assert_eq!(unique_wifi(&root), None);
        std::fs::remove_dir_all(root.join("wrnm012345")).unwrap();
        assert_eq!(unique_wifi(&root).as_deref(), Some("wlp2s0"));
        std::fs::remove_dir_all(&root).unwrap();
    }
}
