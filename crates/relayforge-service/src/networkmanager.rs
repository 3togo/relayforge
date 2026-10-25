//! Native NetworkManager controller. No Python, nmcli or credential-bearing argv.
use crate::{
    nm_journal::{Journal, Owned, Result},
    radio,
};
use relayforge_core::{
    config::Config,
    radio::{Capture, Snapshot},
    Status,
};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value as Json};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    sync::{watch, Mutex},
    time::{sleep, timeout},
};
use zbus::{
    zvariant::{OwnedObjectPath, OwnedValue, Str, Type, Value},
    Connection, Proxy,
};
const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
const PROFILE: &str = "org.freedesktop.NetworkManager.Settings.Connection";
const MARKER: &str = "io.github.relayforge.owner";
// Downstream AP+STA extension advertised by the pinned backend, not a generic NM feature.
const NATIVE_RELAY_CAPABILITY: u32 = 0x7001;
const PARENT: &str = "org.freedesktop.NetworkManager.wifi-relay.parent";
pub(crate) type Settings = HashMap<String, HashMap<String, OwnedValue>>;
pub(crate) fn string(value: &str) -> OwnedValue {
    OwnedValue::from(Str::from(value.to_owned()))
}
fn strings(value: &str) -> OwnedValue {
    OwnedValue::try_from(Value::from(vec![value.to_owned()])).unwrap()
}
/// Neighbour states that prove an address was recently usable on this link.
const ACTIVE_NEIGHBOR_STATES: [&str; 5] = ["REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"];
fn neighbor_states(value: &Json) -> Vec<String> {
    match value.get("state") {
        Some(Json::Array(items)) => items
            .iter()
            .filter_map(Json::as_str)
            .map(str::to_uppercase)
            .collect(),
        Some(Json::String(text)) => text
            .split_whitespace()
            .map(str::to_uppercase)
            .collect(),
        _ => Vec::new(),
    }
}
/// An IPv4 prefix on the shared interface, used to attribute neighbour addresses.
struct Subnet {
    base: u32,
    mask: u32,
}
impl Subnet {
    fn contains(&self, address: std::net::Ipv4Addr) -> bool {
        u32::from(address) & self.mask == self.base
    }
}
fn interface_subnets(text: &str) -> Result<Vec<Subnet>> {
    let interfaces: Vec<Json> =
        serde_json::from_str(text).map_err(|_| "Invalid address inspection")?;
    let mut result = Vec::new();
    for interface in interfaces {
        let addresses = match interface.get("addr_info").and_then(Json::as_array) {
            Some(addresses) => addresses,
            None => continue,
        };
        for address in addresses {
            if address.get("family").and_then(Json::as_str) != Some("inet") {
                continue;
            }
            let (local, prefix) = match (
                address.get("local").and_then(Json::as_str),
                address.get("prefixlen").and_then(Json::as_u64),
            ) {
                (Some(local), Some(prefix)) => (local, prefix),
                _ => continue,
            };
            let Ok(address) = local.parse::<std::net::Ipv4Addr>() else {
                continue;
            };
            if prefix > 32 {
                continue;
            }
            let mask = if prefix == 0 {
                0
            } else {
                u32::MAX << (32 - prefix)
            };
            result.push(Subnet {
                base: u32::from(address) & mask,
                mask,
            });
        }
    }
    Ok(result)
}
fn field<'a>(settings: &'a Settings, section: &str, key: &str) -> Option<&'a str> {
    settings
        .get(section)?
        .get(key)
        .and_then(|v| <&str>::try_from(v).ok())
}
fn profile(config: &Config, owned: &Owned, plan: &relayforge_core::radio::StartPlan) -> Settings {
    let address = HashMap::from([
        ("address".to_owned(), string(&config["GATEWAY"])),
        ("prefix".into(), OwnedValue::from(24u32)),
    ]);
    let mut result = HashMap::from([
        (
            "connection".into(),
            HashMap::from([
                ("id".into(), string("RelayForge Hotspot")),
                ("uuid".into(), string(&owned.uuid)),
                ("type".into(), string("802-11-wireless")),
                ("interface-name".into(), string(&owned.interface)),
                ("autoconnect".into(), false.into()),
            ]),
        ),
        (
            "802-11-wireless".into(),
            HashMap::from([
                (
                    "ssid".into(),
                    OwnedValue::try_from(Value::from(config["SSID"].as_bytes().to_vec())).unwrap(),
                ),
                ("mode".into(), string("ap")),
                (
                    "band".into(),
                    string(if plan.band == "2.4" { "bg" } else { "a" }),
                ),
                ("channel".into(), u32::from(plan.channel.unwrap()).into()),
                ("assigned-mac-address".into(), string("preserve")),
                ("hidden".into(), (config["HIDDEN"] == "1").into()),
                (
                    "ap-isolation".into(),
                    config["ISOLATE_CLIENTS"].parse::<i32>().unwrap().into(),
                ),
            ]),
        ),
        (
            "802-11-wireless-security".into(),
            HashMap::from([
                ("key-mgmt".into(), string("wpa-psk")),
                ("psk".into(), string(&config["PASSPHRASE"])),
                ("proto".into(), strings("rsn")),
                ("pairwise".into(), strings("ccmp")),
                ("group".into(), strings("ccmp")),
                // No psk-flags: 0x2 is NM_SETTING_SECRET_FLAG_NOT_SAVED, which makes
                // NetworkManager drop the supplied PSK and request it from an agent
                // ("no secrets: No agents were available"), so the AP never activates.
                // Persistence is already prevented by persist=volatile.
            ]),
        ),
        (
            "ipv4".into(),
            HashMap::from([
                ("method".into(), string("shared")),
                (
                    "address-data".into(),
                    OwnedValue::try_from(Value::from(vec![address])).unwrap(),
                ),
                ("never-default".into(), true.into()),
            ]),
        ),
        (
            "ipv6".into(),
            HashMap::from([("method".into(), string("disabled"))]),
        ),
    ]);
    let mut tags = HashMap::from([(MARKER.to_owned(), owned.token.clone())]);
    if !owned.helper {
        tags.insert(PARENT.into(), owned.parent.clone());
    }
    result.insert(
        "user".into(),
        HashMap::from([(
            "data".into(),
            OwnedValue::try_from(Value::from(tags)).unwrap(),
        )]),
    );
    result
}
fn matches(settings: &Settings, owned: &Owned) -> bool {
    let tags = settings
        .get("user")
        .and_then(|s| s.get("data"))
        .and_then(|v| v.try_clone().ok())
        .and_then(|v| HashMap::<String, String>::try_from(v).ok());
    field(settings, "connection", "uuid") == Some(&owned.uuid)
        && field(settings, "connection", "id") == Some("RelayForge Hotspot")
        && field(settings, "connection", "interface-name") == Some(&owned.interface)
        && field(settings, "connection", "type") == Some("802-11-wireless")
        && field(settings, "802-11-wireless", "mode") == Some("ap")
        && tags.as_ref().and_then(|t| t.get(MARKER)) == Some(&owned.token)
}
pub(crate) struct Bus {
    connection: Connection,
    owner: String,
}
impl Bus {
    pub(crate) async fn open(fixture: bool) -> Result<Self> {
        let builder = if fixture {
            zbus::connection::Builder::session()
        } else {
            zbus::connection::Builder::system()
        }
        .map_err(|_| "Cannot connect to NetworkManager bus")?;
        let connection = timeout(Duration::from_secs(3), builder.build())
            .await
            .map_err(|_| "NetworkManager bus connection timed out")?
            .map_err(|_| "Cannot connect to NetworkManager bus")?;
        let proxy = zbus::fdo::DBusProxy::new(&connection)
            .await
            .map_err(|_| "Cannot inspect NetworkManager owner")?;
        let owner = timeout(
            Duration::from_secs(3),
            proxy.get_name_owner(NM.try_into().unwrap()),
        )
        .await
        .map_err(|_| "NetworkManager owner lookup timed out")?
        .map_err(|_| "NetworkManager is unavailable")?
        .to_string();
        Ok(Self { connection, owner })
    }
    pub(crate) async fn call<R: DeserializeOwned + Type, B: Serialize + zbus::zvariant::DynamicType>(
        &self,
        path: &str,
        interface: &str,
        method: &str,
        body: &B,
    ) -> Result<R> {
        let proxy = Proxy::new(&self.connection, self.owner.as_str(), path, interface)
            .await
            .map_err(|_| "Invalid NetworkManager endpoint")?;
        timeout(Duration::from_secs(3), proxy.call(method, body))
            .await
            .map_err(|_| "NetworkManager request timed out")?
            .map_err(|_| "NetworkManager request failed")
    }
    pub(crate) async fn property<T: TryFrom<OwnedValue>>(
        &self,
        path: &str,
        interface: &str,
        name: &str,
    ) -> Result<T> {
        let value: OwnedValue = self
            .call(
                path,
                "org.freedesktop.DBus.Properties",
                "Get",
                &(interface, name),
            )
            .await?;
        T::try_from(value).map_err(|_| "Invalid NetworkManager property type")
    }
    pub(crate) async fn device(&self, iface: &str) -> Result<OwnedObjectPath> {
        self.call(ROOT, NM, "GetDeviceByIpIface", &(iface,)).await
    }
    pub(crate) async fn settings(&self, path: &str) -> Result<Settings> {
        self.call(path, PROFILE, "GetSettings", &()).await
    }
    pub(crate) async fn close(self) {
        let _ = timeout(Duration::from_secs(3), self.connection.close()).await;
    }
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
struct Identity {
    ifindex: u32,
    mac: String,
    phy: u32,
}
#[derive(Deserialize)]
struct Interface {
    name: String,
    phy: u32,
    kind: String,
}
#[derive(Clone)]
pub struct Platform {
    pub fixture: bool,
}
impl Platform {
    async fn test<R: DeserializeOwned>(&self, bus: &Bus, method: &str, args: Json) -> Result<R> {
        if !self.fixture {
            return Err("Fixture operation forbidden on system platform");
        }
        let text: String = bus
            .call(
                ROOT,
                "io.github.relayforge.TestNetworkManager",
                method,
                &(args.to_string(),),
            )
            .await?;
        serde_json::from_str(&text).map_err(|_| "Invalid simulated kernel reply")
    }
    pub(crate) async fn inspect(&self, bus: &Bus, iface: &str) -> Result<Snapshot> {
        if self.fixture {
            let capture: Capture = self.test(bus, "Radio", json!({"interface":iface})).await?;
            Snapshot::parse(&capture)
        } else {
            radio::Source::System.inspect(iface).await
        }
    }
    async fn iw(&self, args: &[&str]) -> Result<String> {
        let program = ["/usr/sbin/iw", "/usr/bin/iw", "/sbin/iw", "/bin/iw"]
            .into_iter()
            .map(Path::new)
            .find(|p| p.is_file())
            .ok_or("iw is unavailable")?;
        radio::command(program, args, Duration::from_secs(3)).await
    }
    async fn identity(&self, bus: &Bus, iface: &str) -> Result<Option<Identity>> {
        if self.fixture {
            return self.test(bus, "Identity", json!({"interface":iface})).await;
        }
        let root = PathBuf::from("/sys/class/net").join(iface);
        let index = match std::fs::read_to_string(root.join("ifindex")) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err("Cannot inspect interface identity"),
        };
        let mac = std::fs::read_to_string(root.join("address"))
            .map_err(|_| "Cannot inspect interface MAC")?
            .trim()
            .to_lowercase();
        let info = self.iw(&["dev", iface, "info"]).await?;
        let (phy, _) = relayforge_core::radio::device(&info, iface)?;
        Ok(Some(Identity {
            ifindex: index
                .trim()
                .parse()
                .map_err(|_| "Invalid interface index")?,
            mac,
            phy,
        }))
    }
    async fn interfaces(&self, bus: &Bus) -> Result<Vec<Interface>> {
        if self.fixture {
            return self.test(bus, "Interfaces", json!({})).await;
        }
        let text = self.iw(&["dev"]).await?;
        let mut phy = None;
        let mut name = String::new();
        let mut result = Vec::new();
        for line in text.lines().map(str::trim) {
            if let Some(number) = line.strip_prefix("phy#") {
                phy = Some(
                    number
                        .parse::<u32>()
                        .map_err(|_| "Invalid interface inventory")?,
                );
                name.clear();
            } else if let Some(value) = line.strip_prefix("Interface ") {
                name = value.into();
            } else if line.starts_with("Unnamed/non-netdev interface") {
                name.clear();
            } else if let Some(kind) = line.strip_prefix("type ") {
                result.push(Interface {
                    name: name.clone(),
                    phy: phy.ok_or("Missing inventory PHY")?,
                    kind: kind.into(),
                });
            }
        }
        Ok(result)
    }
    async fn check_subnet(&self, bus: &Bus, gateway: &str) -> Result<()> {
        let routes: Vec<String> = if self.fixture {
            self.test(bus, "Routes", json!({})).await?
        } else {
            let ip = ["/usr/sbin/ip", "/usr/bin/ip", "/sbin/ip", "/bin/ip"]
                .into_iter()
                .map(Path::new)
                .find(|p| p.is_file())
                .ok_or("ip utility is unavailable")?;
            let text = radio::command(
                ip,
                &["-j", "-4", "route", "show", "scope", "link"],
                Duration::from_secs(3),
            )
            .await?;
            let routes: Vec<Json> =
                serde_json::from_str(&text).map_err(|_| "Cannot inspect connected routes")?;
            routes
                .iter()
                .map(|r| {
                    r["dst"]
                        .as_str()
                        .map(str::to_owned)
                        .ok_or("Invalid connected route")
                })
                .collect::<Result<_>>()?
        };
        relayforge_core::subnet::check(gateway, &routes)
    }
    async fn check_identity(&self, bus: &Bus, owned: &Owned) -> Result<()> {
        let identity = self
            .identity(bus, &owned.interface)
            .await?
            .ok_or("Owned interface disappeared")?;
        if identity.phy != owned.phy
            || identity.mac != owned.mac
            || Some(identity.ifindex) != owned.ifindex
        {
            return Err("Owned interface identity changed; refusing operation");
        }
        Ok(())
    }
    async fn create(&self, bus: &Bus, owned: &Owned) -> Result<()> {
        if self.fixture {
            return self.test(bus, "Create", json!(owned)).await;
        }
        self.iw(&[
            "dev",
            &owned.parent,
            "interface",
            "add",
            &owned.interface,
            "type",
            "__ap",
            "addr",
            &owned.mac,
        ])
        .await
        .map(|_| ())
    }
    async fn restore_mode(&self, bus: &Bus, owned: &Owned) -> Result<()> {
        if self.fixture {
            return self.test(bus, "RestoreMode", json!(owned)).await;
        }
        self.iw(&["dev", &owned.interface, "set", "type", "__ap"])
            .await
            .map(|_| ())
    }
    async fn delete(&self, bus: &Bus, owned: &Owned) -> Result<()> {
        if self.fixture {
            return self.test(bus, "Delete", json!(owned)).await;
        }
        self.iw(&["dev", &owned.interface, "del"]).await.map(|_| ())
    }
    async fn verify_ap(&self, bus: &Bus, owned: &Owned, frequency: u32) -> Result<()> {
        if self.fixture {
            return self
                .test(
                    bus,
                    "VerifyAp",
                    json!({"interface":owned.interface,"frequency":frequency}),
                )
                .await;
        }
        let text = self.iw(&["dev", &owned.interface, "info"]).await?;
        let (phy, kind) = relayforge_core::radio::device(&text, &owned.interface)?;
        let matched = text
            .lines()
            .map(str::trim)
            .filter(|line| line.starts_with("channel "))
            .any(|line| {
                line.split_once('(')
                    .and_then(|(_, rest)| rest.split_once(" MHz)"))
                    .and_then(|(v, _)| v.parse::<f64>().ok())
                    .is_some_and(|f| f == f64::from(frequency))
            });
        if phy != owned.phy || kind != "AP" || !matched {
            return Err("AP is not broadcasting on the unchanged upstream channel");
        }
        Ok(())
    }
    async fn clients(&self, bus: &Bus, owned: &Owned) -> Result<Json> {
        if self.fixture {
            return self
                .test(bus, "Clients", json!({"interface":owned.interface}))
                .await;
        }
        let stations = self
            .iw(&["dev", &owned.interface, "station", "dump"])
            .await?;
        let mut macs = Vec::new();
        let mut current = None;
        for line in stations.lines().map(str::trim) {
            if let Some(rest) = line.strip_prefix("Station ") {
                current = rest.split_whitespace().next().map(str::to_owned);
            }
            if line == "authorized:\tyes" || line == "authorized: yes" {
                if let Some(mac) = current.take() {
                    macs.push(mac);
                }
            }
        }
        let ip = ["/usr/sbin/ip", "/usr/bin/ip", "/sbin/ip", "/bin/ip"]
            .into_iter()
            .map(Path::new)
            .find(|p| p.is_file())
            .ok_or("ip utility is unavailable")?;
        let text = radio::command(
            ip,
            &["-j", "neigh", "show", "dev", &owned.interface],
            Duration::from_secs(3),
        )
        .await?;
        let neighbors: Vec<Json> =
            serde_json::from_str(&text).map_err(|_| "Invalid neighbor inspection")?;
        // An address only counts as a lease when the neighbour entry is usable and
        // falls inside the shared subnet; a stale global entry is not evidence.
        let subnets = interface_subnets(
            &radio::command(
                ip,
                &["-j", "-4", "address", "show", "dev", &owned.interface],
                Duration::from_secs(3),
            )
            .await?,
        )?;
        let clients: Vec<_> = macs
            .into_iter()
            .take(1024)
            .map(|mac| {
                let ip = neighbors
                    .iter()
                    .find(|n| {
                        n["lladdr"]
                            .as_str()
                            .is_some_and(|m| m.eq_ignore_ascii_case(&mac))
                            && neighbor_states(n)
                                .iter()
                                .any(|state| ACTIVE_NEIGHBOR_STATES.contains(&state.as_str()))
                    })
                    .and_then(|n| n["dst"].as_str())
                    .filter(|ip| ip.parse::<std::net::Ipv4Addr>().is_ok())
                    .filter(|ip| {
                        subnets.is_empty()
                            || subnets.iter().any(|subnet| {
                                ip.parse::<std::net::Ipv4Addr>()
                                    .is_ok_and(|address| subnet.contains(address))
                            })
                    })
                    .unwrap_or("");
                json!({"mac":mac,"ip":ip,"hostname":""})
            })
            .collect();
        Ok(json!(clients))
    }
}
#[derive(Clone, PartialEq)]
pub(crate) struct Upstream {
    uuid: String,
    ssid: String,
    bssid: String,
    frequency: u32,
    identity: Identity,
}
impl Upstream {
    #[cfg(test)]
    pub(crate) fn test_connection(frequency: u32) -> Self {
        Self {
            uuid: "original-profile".into(),
            ssid: "upstream".into(),
            bssid: "aa:bb:cc:dd:ee:ff".into(),
            frequency,
            identity: Identity {
                ifindex: 7,
                mac: "02:00:00:00:00:07".into(),
                phy: 0,
            },
        }
    }

    pub(crate) fn matches_radio(&self, snapshot: &Snapshot) -> bool {
        self.identity.phy == snapshot.phy
            && self.ssid == snapshot.capabilities.current_sta_ssid
            && self.bssid == snapshot.capabilities.current_sta_bssid
            && self.frequency == snapshot.capabilities.current_sta_frequency
    }
    pub(crate) fn same_connection(&self, other: &Self) -> bool {
        self.uuid == other.uuid
            && self.ssid == other.ssid
            && self.identity == other.identity
            && !self.uuid.is_empty()
            && !self.ssid.is_empty()
            && !other.bssid.is_empty()
            && other.frequency != 0
    }
}
pub(crate) async fn resume_probe(fixture: bool, config: &Config) -> Result<Upstream> {
    let bus = Bus::open(fixture).await?;
    let result = async {
        let (observed, snapshot) =
            upstream(&bus, &Platform { fixture }, &config["WIFI_IFACE"]).await?;
        snapshot.preflight(config)?;
        if !observed.same_connection(&observed) {
            return Err("Original Wi-Fi association is unavailable");
        }
        Ok(observed)
    }
    .await;
    bus.close().await;
    result
}
async fn upstream(bus: &Bus, platform: &Platform, iface: &str) -> Result<(Upstream, Snapshot)> {
    let device = bus.device(iface).await?;
    let state: u32 = bus.property(device.as_str(), DEVICE, "State").await?;
    let capabilities: u32 = bus
        .property(
            device.as_str(),
            "org.freedesktop.NetworkManager.Device.Wireless",
            "WirelessCapabilities",
        )
        .await?;
    if state != 100 || capabilities & 0x40 == 0 {
        return Err("NetworkManager upstream is not connected or lacks AP support");
    }
    let active: OwnedObjectPath = bus
        .property(device.as_str(), DEVICE, "ActiveConnection")
        .await?;
    if active.as_str() == "/" {
        return Err("NetworkManager has no active upstream profile");
    }
    let uuid: String = bus.property(active.as_str(), ACTIVE, "Uuid").await?;
    let snapshot = platform.inspect(bus, iface).await?;
    let identity = platform
        .identity(bus, iface)
        .await?
        .ok_or("Upstream interface disappeared")?;
    let caps = &snapshot.capabilities;
    Ok((
        Upstream {
            uuid,
            ssid: caps.current_sta_ssid.clone(),
            bssid: caps.current_sta_bssid.clone(),
            frequency: caps.current_sta_frequency,
            identity,
        },
        snapshot,
    ))
}
struct Session {
    bus: Option<Bus>,
    owned: Owned,
    baseline: Upstream,
    status: Status,
    active: Option<OwnedObjectPath>,
    clients: Json,
}
pub struct Backend {
    platform: Platform,
    journal: Mutex<Journal>,
    session: Mutex<Option<Session>>,
}
impl Backend {
    pub fn new(platform: Platform, journal: Journal) -> Arc<Self> {
        Arc::new(Self {
            platform,
            journal: Mutex::new(journal),
            session: Mutex::new(None),
        })
    }
    async fn recover_inner(&self) -> Result<()> {
        let record = self.journal.lock().await.load()?;
        let Some(owned) = record else { return Ok(()) };
        let bus = Bus::open(self.platform.fixture).await?;
        // Verify the helper's kernel identity before any profile deletion that
        // could affect an interface whose name has since been reused.
        if let Some(identity) = self.platform.identity(&bus, &owned.interface).await? {
            if (owned.helper || owned.ifindex.is_some())
                && (identity.phy != owned.phy
                    || identity.mac != owned.mac
                    || owned.ifindex.is_some_and(|index| index != identity.ifindex))
            {
                return Err("Owned interface identity changed; refusing cleanup");
            }
        }
        let paths: Vec<OwnedObjectPath> = bus
            .call(
                "/org/freedesktop/NetworkManager/Settings",
                "org.freedesktop.NetworkManager.Settings",
                "ListConnections",
                &(),
            )
            .await?;
        for path in paths {
            let settings = bus.settings(path.as_str()).await?;
            if field(&settings, "connection", "uuid") == Some(&owned.uuid) {
                if !matches(&settings, &owned) {
                    return Err("Owned NetworkManager profile identity changed; refusing cleanup");
                }
                let _: () = bus.call(path.as_str(), PROFILE, "Delete", &()).await?;
            }
        }
        if owned.helper {
            if let Some(identity) = self.platform.identity(&bus, &owned.interface).await? {
                if identity.phy != owned.phy
                    || identity.mac != owned.mac
                    || owned.ifindex.is_some_and(|index| index != identity.ifindex)
                {
                    return Err("Owned interface identity changed; refusing cleanup");
                }
                self.platform.delete(&bus, &owned).await?;
            }
        }
        for _ in 0..20 {
            let paths: Vec<OwnedObjectPath> = bus
                .call(
                    "/org/freedesktop/NetworkManager/Settings",
                    "org.freedesktop.NetworkManager.Settings",
                    "ListConnections",
                    &(),
                )
                .await?;
            let mut profile_present = false;
            for path in paths {
                let settings = bus.settings(path.as_str()).await?;
                if field(&settings, "connection", "uuid") == Some(&owned.uuid) {
                    profile_present = true;
                }
            }
            if !profile_present
                && self
                    .platform
                    .identity(&bus, &owned.interface)
                    .await?
                    .is_none()
            {
                self.journal.lock().await.clear()?;
                bus.close().await;
                return Ok(());
            }
            sleep(Duration::from_millis(100)).await;
        }
        bus.close().await;
        Err("Native NetworkManager cleanup is unconfirmed; ownership journal retained")
    }
    pub async fn recover(&self) -> Result<()> {
        timeout(Duration::from_secs(25), self.recover_inner())
            .await
            .map_err(|_| "Native NetworkManager recovery timed out")?
    }
    async fn cleanup(&self, session: &mut Session) -> Result<()> {
        if let Some(bus) = session.bus.take() {
            // The private connection is bound to this activation. Closing it also
            // handles ambiguous timeout/cancellation of AddAndActivateConnection2.
            if let Some(active) = &session.active {
                if let Ok(profile_path) = bus
                    .property::<OwnedObjectPath>(active.as_str(), ACTIVE, "Connection")
                    .await
                {
                    if bus
                        .settings(profile_path.as_str())
                        .await
                        .is_ok_and(|s| matches(&s, &session.owned))
                    {
                        let _: Result<()> =
                            bus.call(ROOT, NM, "DeactivateConnection", &(active,)).await;
                    }
                }
            }
            bus.close().await;
        }
        self.recover().await
    }
    async fn activate(&self, session: &mut Session, config: &Config) -> Result<()> {
        let bus = session.bus.as_ref().unwrap();
        let (fresh, snapshot) = upstream(bus, &self.platform, &config["WIFI_IFACE"]).await?;
        let plan = snapshot.preflight(config)?;
        if fresh != session.baseline {
            return Err("Upstream changed before activation");
        }
        let all = self.platform.interfaces(bus).await?;
        if !all.iter().any(|i| {
            i.name == config["WIFI_IFACE"] && i.phy == fresh.identity.phy && i.kind == "managed"
        }) || all.iter().any(|i| {
            i.phy == fresh.identity.phy
                && i.name != config["WIFI_IFACE"]
                && !["P2P-device", "monitor", "AP/VLAN"].contains(&i.kind.as_str())
        }) {
            return Err("Another AP or client occupies the selected PHY");
        }
        if self
            .platform
            .identity(bus, &session.owned.interface)
            .await?
            .is_some()
        {
            return Err("Proposed owned interface already exists");
        }
        self.platform.check_subnet(bus, &config["GATEWAY"]).await?;
        self.journal.lock().await.write(&session.owned)?;
        let device = if session.owned.helper {
            self.platform.create(bus, &session.owned).await?;
            let identity = self
                .platform
                .identity(bus, &session.owned.interface)
                .await?
                .ok_or("Created interface disappeared")?;
            if identity.mac != session.owned.mac || identity.phy != session.owned.phy {
                return Err("Created interface identity differs from intent");
            }
            session.owned.ifindex = Some(identity.ifindex);
            self.journal.lock().await.write(&session.owned)?;
            let device = loop {
                if let Ok(device) = bus.device(&session.owned.interface).await {
                    break device;
                }
                sleep(Duration::from_millis(100)).await;
            };
            self.platform.check_identity(bus, &session.owned).await?;
            let _: () = bus
                .call(
                    device.as_str(),
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    &(DEVICE, "Autoconnect", Value::from(false)),
                )
                .await?;
            let _: () = bus
                .call(
                    device.as_str(),
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    &(DEVICE, "Managed", Value::from(true)),
                )
                .await?;
            let mut restored = false;
            loop {
                let state: u32 = bus.property(device.as_str(), DEVICE, "State").await?;
                if state == 30 {
                    break;
                }
                if state > 30 {
                    return Err("Virtual AP device did not become ready");
                }
                self.ensure_upstream(session).await?;
                if state == 20 && !restored {
                    self.platform.check_identity(bus, &session.owned).await?;
                    self.platform.restore_mode(bus, &session.owned).await?;
                    restored = true;
                }
                sleep(Duration::from_millis(100)).await;
            }
            device
        } else {
            OwnedObjectPath::try_from("/").unwrap()
        };
        self.ensure_upstream(session).await?;
        let siblings = self.platform.interfaces(bus).await?;
        if siblings.iter().any(|i| {
            i.phy == session.owned.phy
                && i.name != session.owned.parent
                && i.name != session.owned.interface
                && !["P2P-device", "monitor", "AP/VLAN"].contains(&i.kind.as_str())
        }) {
            return Err("Another AP or client appeared before activation");
        }
        if session.owned.helper {
            self.platform.check_identity(bus, &session.owned).await?;
        }
        self.platform.check_subnet(bus, &config["GATEWAY"]).await?;
        let options = HashMap::from([
            ("persist", string("volatile")),
            ("bind-activation", string("dbus-client")),
        ]);
        let (profile_path, active, _): (
            OwnedObjectPath,
            OwnedObjectPath,
            HashMap<String, OwnedValue>,
        ) = bus
            .call(
                ROOT,
                NM,
                "AddAndActivateConnection2",
                &(
                    profile(config, &session.owned, &plan),
                    device,
                    OwnedObjectPath::try_from("/").unwrap(),
                    options,
                ),
            )
            .await?;
        if active.as_str() == "/"
            || !matches(&bus.settings(profile_path.as_str()).await?, &session.owned)
        {
            return Err("NetworkManager returned a mismatched owned profile");
        }
        session.active = Some(active);
        loop {
            self.ensure_upstream(session).await?;
            // A rejected activation removes the ActiveConnection, so a vanished object
            // must read as an activation failure rather than an opaque transport error.
            let state: u32 = match bus
                .property(session.active.as_ref().unwrap().as_str(), ACTIVE, "State")
                .await
            {
                Ok(state) => state,
                Err(_) => {
                    return Err("Native NetworkManager activation was rejected; see the journal")
                }
            };
            if state == 2 {
                break;
            }
            if state >= 3 {
                return Err("Native NetworkManager activation failed or was disconnected");
            }
            sleep(Duration::from_millis(100)).await;
        }
        self.platform
            .verify_ap(bus, &session.owned, session.baseline.frequency)
            .await?;
        let identity = self
            .platform
            .identity(bus, &session.owned.interface)
            .await?
            .ok_or("Active AP interface disappeared")?;
        if identity.phy != session.owned.phy
            || session.owned.helper
                && (identity.mac != session.owned.mac
                    || Some(identity.ifindex) != session.owned.ifindex)
        {
            return Err("Active AP identity changed");
        }
        if !session.owned.helper {
            session.owned.mac = identity.mac;
            session.owned.ifindex = Some(identity.ifindex);
            self.journal.lock().await.write(&session.owned)?;
        }
        session.status.active = true;
        session.status.state = "active".into();
        Ok(())
    }
    async fn ensure_upstream(&self, session: &Session) -> Result<()> {
        let (observed, _) = upstream(
            session
                .bus
                .as_ref()
                .ok_or("NetworkManager session disconnected")?,
            &self.platform,
            &session.owned.parent,
        )
        .await?;
        if observed != session.baseline {
            return Err("Upstream connection changed; sharing stopped without reconnecting it");
        }
        Ok(())
    }
    pub async fn start(
        &self,
        config: &Config,
        mut cancelled: watch::Receiver<u64>,
        expected: Option<&Upstream>,
    ) -> Json {
        if config.get("BACKEND").map(String::as_str) != Some("networkmanager") {
            return json!({"success":false,"error":"Native mode requires BACKEND=networkmanager"});
        }
        let mut slot = self.session.lock().await;
        if let Some(session) = slot.as_ref() {
            return if session.status.active
                && session.status.state == "active"
                && session.bus.is_some()
            {
                json!({"success":true,"status":session.status})
            } else {
                json!({"success":false,"error":"Stop must confirm native cleanup before restarting"})
            };
        }
        if let Err(error) = self.recover().await {
            return json!({"success":false,"error":error});
        }
        let prepared = async {
            let bus = Bus::open(self.platform.fixture).await?;
            let (baseline, snapshot) =
                upstream(&bus, &self.platform, &config["WIFI_IFACE"]).await?;
            snapshot.preflight(config)?;
            if expected.is_some_and(|expected| *expected != baseline) {
                return Err("Original upstream changed before resume");
            }
            let caps: Vec<u32> = bus.property(ROOT, NM, "Capabilities").await?;
            let owned = Owned::new(
                &config["WIFI_IFACE"],
                baseline.identity.phy,
                !caps.contains(&NATIVE_RELAY_CAPABILITY),
            )?;
            let status = Status {
                backend: "networkmanager".into(),
                state: "connecting".into(),
                desired_active: true,
                iface: owned.interface.clone(),
                phy_iface: owned.parent.clone(),
                ssid: config["SSID"].clone(),
                band: snapshot.capabilities.current_sta_band.clone(),
                ..Status::default()
            };
            Ok::<_, &'static str>(Session {
                bus: Some(bus),
                owned,
                baseline,
                status,
                active: None,
                clients: json!([]),
            })
        };
        let prepared = tokio::select! {result=prepared=>result,_=cancelled.changed()=>Err("Startup cancelled")};
        let mut session = match prepared {
            Ok(session) => session,
            Err(error) => return json!({"success":false,"error":error}),
        };
        let result = tokio::select! {result=timeout(Duration::from_secs(45),self.activate(&mut session,config))=>result.map_err(|_|"Native NetworkManager startup timed out").and_then(|r|r),_=cancelled.changed()=>Err("Startup cancelled")};
        match result {
            Ok(()) => {
                let reply = json!({"success":true,"status":session.status});
                *slot = Some(session);
                reply
            }
            Err(error) => {
                let cleanup = self.cleanup(&mut session).await;
                if cleanup.is_err() {
                    session.status.error =
                        Some("Native cleanup unconfirmed; Stop must retry".into());
                    session.status.state = "failed".into();
                    session.status.desired_active = false;
                    *slot = Some(session);
                }
                json!({"success":false,"error":if cleanup.is_ok(){error}else{"Native startup failed and cleanup is unconfirmed; Stop must retry"}})
            }
        }
    }
    pub async fn stop(&self) -> bool {
        let mut slot = self.session.lock().await;
        let result = if let Some(session) = slot.as_mut() {
            self.cleanup(session).await
        } else {
            self.recover().await
        };
        if result.is_ok() {
            *slot = None;
            true
        } else {
            if let Some(session) = slot.as_mut() {
                session.status.state = "failed".into();
                session.status.desired_active = false;
                session.status.error = Some("Native cleanup unconfirmed; Stop must retry".into());
            }
            false
        }
    }
    pub async fn poll(&self) -> Option<(Status, Json)> {
        let mut slot = self.session.try_lock().ok()?;
        let session = slot.as_mut()?;
        if !session.status.active {
            return Some((session.status.clone(), json!([])));
        }
        let inspect = async {
            self.ensure_upstream(session).await?;
            let bus = session
                .bus
                .as_ref()
                .ok_or("NetworkManager bus disconnected")?;
            if bus
                .property::<u32>(
                    session
                        .active
                        .as_ref()
                        .ok_or("Missing active connection")?
                        .as_str(),
                    ACTIVE,
                    "State",
                )
                .await?
                != 2
            {
                return Err("Sharing was disconnected; explicit Start is required");
            }
            self.platform
                .verify_ap(bus, &session.owned, session.baseline.frequency)
                .await?;
            self.platform.clients(bus, &session.owned).await
        };
        let result = timeout(Duration::from_secs(8), inspect)
            .await
            .map_err(|_| "Native NetworkManager monitoring timed out")
            .and_then(|r| r);
        match result {
            Ok(clients)
                if relayforge_core::validate_reply(
                    relayforge_core::Method::GetClients,
                    &clients,
                )
                .is_ok() =>
            {
                session.status.client_count = clients.as_array().unwrap().len() as u32;
                session.clients = clients.clone();
                Some((session.status.clone(), clients))
            }
            failure => {
                let error = failure.err().unwrap_or("Invalid native client observation");
                let clean = self.cleanup(session).await.is_ok();
                let status = Status {
                    active: session.status.active && !clean,
                    desired_active: false,
                    client_count: if clean {
                        0
                    } else {
                        session.status.client_count
                    },
                    state: "failed".into(),
                    error: Some(
                        if clean {
                            error
                        } else {
                            "Native monitoring failed; cleanup is unconfirmed"
                        }
                        .into(),
                    ),
                    ..session.status.clone()
                };
                let clients = if clean {
                    json!([])
                } else {
                    session.clients.clone()
                };
                if clean {
                    *slot = None;
                } else {
                    session.status = status.clone();
                }
                Some((status, clients))
            }
        }
    }
}
