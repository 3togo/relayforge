//! AP allocation and per-device NetworkManager ownership. No global NM changes.
use super::*;
use zbus::{
    zvariant::{OwnedObjectPath, OwnedValue, Value},
    Connection, Proxy,
};
const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SimDevice {
    pub(super) path: String,
    pub(super) managed: bool,
    pub(super) state: u32,
    pub(super) active: bool,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SimManager {
    pub(super) owner: String,
    pub(super) devices: BTreeMap<String, SimDevice>,
    #[serde(default)]
    pub(super) omit_created: bool,
}
struct Manager<'a> {
    p: &'a Platform,
    owner: Option<String>,
    connection: Option<Connection>,
}
impl<'a> Manager<'a> {
    async fn open(p: &'a Platform) -> Result<Self> {
        if p.fixture.is_some() && !(Kernel { p }).simulation()?.1.nm_bus {
            let owner = Kernel { p }.simulation()?.1.nm.map(|v| v.owner);
            return Ok(Self {
                p,
                owner,
                connection: None,
            });
        }
        let connection = tokio::time::timeout(Duration::from_secs(3), async {
            if p.fixture.is_some() {
                Connection::session().await
            } else {
                Connection::system().await
            }
        })
        .await
        .map_err(|_| "NetworkManager bus timed out")?
        .map_err(|_| "NetworkManager bus unavailable")?;
        let proxy = zbus::fdo::DBusProxy::new(&connection)
            .await
            .map_err(|_| "Cannot inspect NetworkManager")?;
        let owner = match tokio::time::timeout(
            Duration::from_secs(3),
            proxy.get_name_owner(NM.try_into().unwrap()),
        )
        .await
        .map_err(|_| "NetworkManager owner lookup timed out")?
        {
            Ok(value) => Some(value.to_string()),
            Err(zbus::fdo::Error::NameHasNoOwner(_)) => None,
            Err(_) => return Err("Cannot inspect NetworkManager owner"),
        };
        Ok(Self {
            p,
            owner,
            connection: Some(connection),
        })
    }
    async fn call<
        R: serde::de::DeserializeOwned + zbus::zvariant::Type,
        B: Serialize + zbus::zvariant::DynamicType,
    >(
        &self,
        path: &str,
        interface: &str,
        method: &str,
        body: &B,
    ) -> Result<R> {
        let proxy = Proxy::new(
            self.connection
                .as_ref()
                .ok_or("Missing NetworkManager bus")?,
            self.owner.as_deref().ok_or("NetworkManager disappeared")?,
            path,
            interface,
        )
        .await
        .map_err(|_| "Invalid NetworkManager endpoint")?;
        tokio::time::timeout(Duration::from_secs(2), proxy.call(method, body))
            .await
            .map_err(|_| "NetworkManager request timed out")?
            .map_err(|_| "NetworkManager request failed")
    }
    async fn property<T: TryFrom<OwnedValue>>(&self, path: &str, name: &str) -> Result<T> {
        let v: OwnedValue = self
            .call(
                path,
                "org.freedesktop.DBus.Properties",
                "Get",
                &(DEVICE, name),
            )
            .await?;
        T::try_from(v).map_err(|_| "Invalid NetworkManager property")
    }
    async fn observe(&self, iface: &str) -> Result<Option<SimDevice>> {
        if self.owner.is_none() {
            return Ok(None);
        }
        if self.connection.is_none() {
            let state = Kernel { p: self.p }
                .simulation()?
                .1
                .nm
                .ok_or("NetworkManager disappeared")?;
            if Some(&state.owner) != self.owner.as_ref() {
                return Err("NetworkManager restarted");
            }
            return Ok(state.devices.get(iface).cloned());
        }
        let paths: Vec<OwnedObjectPath> = self.call(ROOT, NM, "GetDevices", &()).await?;
        if paths.len() > 128 {
            return Err("Too many NetworkManager devices");
        }
        let mut found = None;
        for path in paths {
            let name: String = self.property(path.as_str(), "Interface").await?;
            if name != iface {
                continue;
            }
            if found.is_some() {
                return Err("Ambiguous NetworkManager device");
            }
            let active: OwnedObjectPath = self.property(path.as_str(), "ActiveConnection").await?;
            found = Some(SimDevice {
                path: path.to_string(),
                managed: self.property(path.as_str(), "Managed").await?,
                state: self.property(path.as_str(), "State").await?,
                active: active.as_str() != "/",
            });
        }
        Ok(found)
    }
    async fn set(&self, iface: &str, expected: &SimDevice, value: bool) -> Result<()> {
        let actual = self
            .observe(iface)
            .await?
            .ok_or("NetworkManager device disappeared")?;
        if actual.path != expected.path || actual.active || ![10, 20, 30].contains(&actual.state) {
            return Err("NetworkManager device is active or changed");
        }
        if self.connection.is_none() {
            return Kernel { p: self.p }.simulate(
                vec![
                    "NetworkManager".into(),
                    iface.into(),
                    "Managed".into(),
                    value.to_string(),
                ],
                |s| {
                    let manager = s.nm.as_mut().ok_or("Missing simulated NetworkManager")?;
                    if Some(&manager.owner) != self.owner.as_ref() {
                        return Err("NetworkManager restarted");
                    }
                    let device = manager
                        .devices
                        .get_mut(iface)
                        .ok_or("Missing NetworkManager device")?;
                    device.managed = value;
                    device.state = if value { 30 } else { 10 };
                    Ok(())
                },
            );
        }
        self.call(
            expected.path.as_str(),
            "org.freedesktop.DBus.Properties",
            "Set",
            &(DEVICE, "Managed", Value::from(value)),
        )
        .await
    }
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct NmIntent {
    owner: String,
    path: String,
    baseline: bool,
    restored: bool,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    version: u32,
    owner: Identity,
    marker: String,
    parent: String,
    parent_identity: Interface,
    phy: u32,
    iface: String,
    mac: String,
    virtual_ap: bool,
    created: Option<Interface>,
    nm: Option<NmIntent>,
    released: bool,
    restored: bool,
}
impl Intent {
    fn validate(&self, data: &Snapshot) -> Result<()> {
        interface_name(&self.parent)?;
        interface_name(&self.iface)?;
        valid_mac(&self.mac)?;
        if self.version != 1
            || self.owner != data.owner
            || self.marker != data.fields["DAEMON_PIDFILE"]
            || self.iface != data.fields["WIFI_IFACE"]
            || self.virtual_ap != (data.fields["NO_VIRT"] == "0")
            || (self.virtual_ap
                && (self.iface != data.fields["VWIFI_IFACE"]
                    || self.mac != data.fields["NEW_MACADDR"]
                    || self.iface == self.parent))
            || (!self.virtual_ap && self.iface != self.parent)
            || self.parent_identity.ifindex == 0
            || data.interfaces.get(&self.parent) != Some(&self.parent_identity)
        {
            return Err("Device intent ownership changed");
        }
        valid_mac(&self.parent_identity.mac)?;
        if let Some(id) = &self.created {
            valid_mac(&id.mac)?;
            if id.ifindex == 0 || id.mac != self.mac {
                return Err("Invalid created device identity");
            }
        }
        if let Some(nm) = &self.nm {
            if zbus::names::UniqueName::try_from(nm.owner.as_str()).is_err()
                || nm.owner.len() > 255
                || zbus::zvariant::ObjectPath::try_from(nm.path.as_str()).is_err()
                || !nm
                    .path
                    .starts_with("/org/freedesktop/NetworkManager/Devices/")
                || nm.path.len() > 255
            {
                return Err("Invalid NetworkManager ownership intent");
            }
        }
        Ok(())
    }
    fn parent(&self, p: &Platform) -> Result<()> {
        if p.interface(&self.parent)?.as_ref() != Some(&self.parent_identity) {
            return Err("Parent interface identity changed");
        }
        Ok(())
    }
    async fn identity(&self, p: &Platform) -> Result<Option<Netdev>> {
        let k = Kernel { p };
        let dev = k.inspect(&self.iface).await?;
        if dev.is_none() && self.virtual_ap && k.macs().await?.contains(&self.mac) {
            return Err("The planned AP MAC exists under another interface identity");
        }
        if let Some(v) = &dev {
            if v.identity.mac != self.mac
                || self.created.as_ref().is_some_and(|id| id != &v.identity)
                || k.phy(&self.iface).await? != self.phy
            {
                return Err("Created device identity changed");
            }
        }
        Ok(dev)
    }
}
fn read(dir: &Directory, data: &Snapshot) -> Result<Option<(Intent, Vec<u8>)>> {
    dir.read("device.json", true, LIMIT)?
        .map(|bytes| {
            let intent: Intent =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid device journal")?;
            intent.validate(data)?;
            Ok((intent, bytes))
        })
        .transpose()
}
pub(super) fn fields(data: &Snapshot) -> String {
    [
        "WIFI_IFACE",
        "VWIFI_IFACE",
        "NEW_MACADDR",
        "OLD_MACADDR",
        "ADDED_UNMANAGED",
    ]
    .iter()
    .map(|k| format!("{k}={}", quote(&data.fields[*k])))
    .collect::<Vec<_>>()
    .join("\n")
}
async fn create(p: &Platform, dir: &Directory) -> Result<String> {
    let (mut data, mut snapshot_bytes) = snapshot(p, dir)?;
    if data.owner.current()?.is_none() {
        return Err("Device owner exited");
    }
    if data.fields["SHARE_METHOD"] == "bridge" || !data.fields["ADDED_UNMANAGED"].is_empty() {
        return Err("Unsupported historical device state");
    }
    let kernel = Kernel { p };
    let root = Directory::open(&p.root)?;
    let _allocation = root.transaction_lock(c".allocation.lock").await?;
    let existing = read(dir, &data)?;
    let fresh = existing.is_none();
    let mut intent = if let Some((v, _)) = existing {
        v
    } else {
        if !data.fields["VWIFI_IFACE"].is_empty() {
            return Err("Historical AP creation requires its matching adapter");
        }
        let parent = data.fields["WIFI_IFACE"].clone();
        let identity = p.interface(&parent)?.ok_or("Parent interface missing")?;
        if data.interfaces.get(&parent) != Some(&identity) {
            return Err("Parent interface changed");
        }
        let phy = kernel.phy(&parent).await?;
        let virtual_ap = data.fields["NO_VIRT"] == "0";
        let (iface, mac) = if virtual_ap {
            let mut candidate = None;
            for _ in 0..16 {
                let token = random_secret()?;
                let name = format!("rfap{}", &token[..8]);
                let mac = format!(
                    "02:{}:{}:{}:{}:{}",
                    &token[8..10],
                    &token[10..12],
                    &token[12..14],
                    &token[14..16],
                    &token[16..18]
                );
                if p.interface(&name)?.is_none() && !kernel.macs().await?.contains(&mac) {
                    candidate = Some((name, mac));
                    break;
                }
            }
            candidate.ok_or("No unused AP identity available")?
        } else {
            (parent.clone(), identity.mac.clone())
        };
        Intent {
            version: 1,
            owner: data.owner.clone(),
            marker: data.fields["DAEMON_PIDFILE"].clone(),
            parent,
            parent_identity: identity,
            phy,
            iface,
            mac,
            virtual_ap,
            created: None,
            nm: None,
            released: false,
            restored: false,
        }
    };
    if intent.restored {
        return Err("Finish prior device cleanup before creating again");
    }
    intent.parent(p)?;
    if fresh {
        data.fields
            .insert("WIFI_IFACE".into(), intent.iface.clone());
        data.fields.insert(
            "VWIFI_IFACE".into(),
            if intent.virtual_ap {
                intent.iface.clone()
            } else {
                String::new()
            },
        );
        data.fields.insert(
            "NEW_MACADDR".into(),
            if intent.virtual_ap {
                intent.mac.clone()
            } else {
                data.fields["NEW_MACADDR"].clone()
            },
        );
        data.fields
            .insert("OLD_MACADDR".into(), intent.parent_identity.mac.clone());
        // Snapshot first: a crash before intent creation cannot have launched iw.
        data.validate(p, &dir.path)?;
        dir.write("recovery.json", &data, Some(&snapshot_bytes))?;
        snapshot_bytes = dir
            .read("recovery.json", true, LIMIT)?
            .ok_or("Missing device snapshot")?;
        dir.write("device.json", &intent, None)?;
    }
    intent.validate(&data)?;
    if intent.virtual_ap && intent.identity(p).await?.is_none() {
        if intent.created.is_some() {
            return Err("Previously created AP disappeared");
        }
        intent.parent(p)?;
        if kernel.phy(&intent.parent).await? != intent.phy {
            return Err("Parent radio identity changed before creation");
        }
        if data.owner.current()?.is_none() {
            return Err("Device owner exited before creation");
        }
        kernel
            .create(&intent.parent, &intent.iface, &intent.mac, intent.phy)
            .await?;
    }
    let current = intent
        .identity(p)
        .await?
        .ok_or("Created interface missing")?;
    if intent.virtual_ap && current.kind != "AP" {
        return Err("Created AP mode changed");
    }
    if intent.created.is_none() {
        let bytes = dir
            .read("device.json", true, LIMIT)?
            .ok_or("Missing device intent")?;
        intent.created = Some(current.identity.clone());
        dir.write("device.json", &intent, Some(&bytes))?;
    }
    data.interfaces
        .insert(intent.iface.clone(), current.identity);
    dir.write("recovery.json", &data, Some(&snapshot_bytes))?;
    Ok(fields(&data))
}
async fn release(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    if data.owner.current()?.is_none() {
        return Err("Device owner exited");
    }
    let (mut intent, mut bytes) = read(dir, data)?.ok_or("Missing device ownership intent")?;
    if intent.restored {
        return Err("Device already restored");
    }
    let dev = intent
        .identity(p)
        .await?
        .ok_or("AP missing before NetworkManager release")?;
    if dev.connected || dev.addresses.iter().any(|v| nonlocal(v)) {
        return Err("AP is already connected or configured");
    }
    if intent.iface == data.fields["INTERNET_IFACE"] {
        return Err("Cannot unmanage the upstream device");
    }
    let manager = Manager::open(p).await?;
    let mut observed = manager.observe(&intent.iface).await?;
    if manager.owner.is_some() && observed.is_none() {
        for _ in 0..20 {
            sleep(Duration::from_millis(100)).await;
            observed = manager.observe(&intent.iface).await?;
            if observed.is_some() {
                break;
            }
        }
        if observed.is_none() {
            return Err("NetworkManager has not discovered the AP");
        }
    }
    if let Some(actual) = observed {
        if actual.active || ![10, 20, 30].contains(&actual.state) {
            return Err("NetworkManager AP is active");
        }
        if let Some(nm) = &intent.nm {
            if manager.owner.as_deref() != Some(nm.owner.as_str()) || actual.path != nm.path {
                return Err("NetworkManager ownership changed");
            }
        } else {
            intent.nm = Some(NmIntent {
                owner: manager.owner.clone().unwrap(),
                path: actual.path.clone(),
                baseline: actual.managed,
                restored: false,
            });
            dir.write("device.json", &intent, Some(&bytes))?;
            bytes = dir
                .read("device.json", true, LIMIT)?
                .ok_or("Missing device intent")?;
        }
        if actual.managed {
            intent
                .identity(p)
                .await?
                .ok_or("AP disappeared before release")?;
            if data.owner.current()?.is_none() {
                return Err("Device owner exited before release");
            }
            manager.set(&intent.iface, &actual, false).await?;
        }
        let mut confirmed = false;
        for _ in 0..20 {
            if let Some(v) = manager.observe(&intent.iface).await? {
                if v.path == actual.path && !v.managed && !v.active && v.state == 10 {
                    confirmed = true;
                    break;
                }
            }
            sleep(Duration::from_millis(100)).await;
        }
        if !confirmed {
            return Err("NetworkManager release is unconfirmed");
        }
    } else if intent.nm.is_some() {
        return Err("Recorded NetworkManager disappeared");
    }
    intent.released = true;
    dir.write("device.json", &intent, Some(&bytes))
}
async fn restore(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    let Some((mut intent, bytes)) = read(dir, data)? else {
        return if data.fields["ADDED_UNMANAGED"].is_empty() {
            Ok(())
        } else {
            Err("Historical NetworkManager ownership requires its matching adapter")
        };
    };
    if intent.restored {
        return Ok(());
    }
    let dev = intent.identity(p).await?;
    if intent.virtual_ap && dev.is_some() {
        return Err("Remove the owned AP before restoring NetworkManager");
    }
    if !intent.virtual_ap && dev.is_some() {
        let proof = dir.read("interface.json", true, LIMIT)?;
        if let Some(bytes) = proof {
            let baseline: InterfaceIntent = serde_json::from_slice(&bytes)
                .map_err(|_| "Invalid interface restoration proof")?;
            baseline.validate(data)?;
            if !baseline.restored {
                return Err("Restore the dedicated AP before NetworkManager");
            }
        } else if data.fields["PHYSICAL_CONFIGURED"] == "1" {
            return Err("Missing dedicated AP restoration proof");
        }
        if let Some(nm) = &intent.nm {
            if !nm.restored && nm.baseline {
                let manager = Manager::open(p).await?;
                if manager.owner.as_deref() != Some(nm.owner.as_str()) {
                    return Err("NetworkManager restarted before restoration");
                }
                let actual = manager
                    .observe(&intent.iface)
                    .await?
                    .ok_or("NetworkManager device missing")?;
                if actual.path != nm.path {
                    return Err("NetworkManager device identity changed");
                }
                if !actual.managed {
                    let dev = intent
                        .identity(p)
                        .await?
                        .ok_or("Dedicated device missing")?;
                    if dev.connected || actual.active || ![10, 20, 30].contains(&actual.state) {
                        return Err("Dedicated device acquired another connection");
                    }
                    manager.set(&intent.iface, &actual, true).await?;
                }
                if !manager
                    .observe(&intent.iface)
                    .await?
                    .is_some_and(|v| v.path == nm.path && v.managed)
                {
                    return Err("NetworkManager restoration unconfirmed");
                }
            }
        }
    }
    if let Some(nm) = &mut intent.nm {
        nm.restored = true;
    }
    intent.restored = true;
    dir.write("device.json", &intent, Some(&bytes))
}
pub(super) async fn reconcile(
    p: &Platform,
    dir: &Directory,
    data: &mut Snapshot,
    bytes: &[u8],
) -> Result<()> {
    if let Some((mut intent, journal)) = read(dir, data)? {
        if !intent.virtual_ap || intent.restored {
            return Ok(());
        }
        if let Some(dev) = intent.identity(p).await? {
            if intent.created.is_none() {
                intent.parent(p)?;
                if dev.kind != "AP" || dev.connected {
                    return Err("Unconfirmed AP creation changed");
                }
                intent.created = Some(dev.identity.clone());
                dir.write("device.json", &intent, Some(&journal))?;
            }
            if data.interfaces.get(&intent.iface) != Some(&dev.identity) {
                data.interfaces.insert(intent.iface.clone(), dev.identity);
                dir.write("recovery.json", data, Some(bytes))?;
            }
        }
    }
    Ok(())
}
pub(super) fn check(dir: &Directory, data: &Snapshot) -> Result<()> {
    if let Some((intent, _)) = read(dir, data)? {
        if !intent.restored {
            return Err("Device ownership still needs restoration");
        }
    } else if !data.fields["ADDED_UNMANAGED"].is_empty() {
        return Err("Unconfirmed historical NetworkManager ownership");
    }
    Ok(())
}
pub(super) async fn action(p: &Platform, dir: &Directory, operation: &str) -> Result<String> {
    let _lock = dir.transaction_lock(c".network.lock").await?;
    if operation == "interface-create" {
        return create(p, dir).await;
    }
    let (data, _) = snapshot(p, dir)?;
    data.interfaces(p)?;
    match operation {
        "device-fields" => Ok(fields(&data)),
        "device-release" => {
            release(p, dir, &data).await?;
            Ok(String::new())
        }
        "device-restore" => {
            restore(p, dir, &data).await?;
            Ok(String::new())
        }
        _ => Err("Unknown device operation"),
    }
}

pub(super) async fn require_release(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    if let Some((intent, _)) = read(dir, data)? {
        if !intent.released || intent.restored {
            return Err("Release the AP from NetworkManager before configuring it");
        }
        intent
            .identity(p)
            .await?
            .ok_or("AP disappeared before configuration")?;
        let manager = Manager::open(p).await?;
        if let Some(nm) = &intent.nm {
            if manager.owner.as_deref() != Some(nm.owner.as_str()) {
                return Err("NetworkManager ownership changed before configuration");
            }
            let actual = manager
                .observe(&intent.iface)
                .await?
                .ok_or("NetworkManager AP disappeared")?;
            if actual.path != nm.path || actual.managed || actual.active || actual.state != 10 {
                return Err("NetworkManager reclaimed the AP");
            }
        } else if manager.owner.is_some() {
            return Err("NetworkManager appeared after AP release");
        }
    }
    Ok(())
}
pub(super) async fn permit_remove(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    if let Some((intent, _)) = read(dir, data)? {
        if intent.restored || intent.identity(p).await?.is_none() {
            return Ok(());
        }
        let manager = Manager::open(p).await?;
        if let Some(nm) = &intent.nm {
            if manager.owner.as_deref() != Some(nm.owner.as_str()) {
                return Err("NetworkManager ownership changed before removal");
            }
        }
        if let Some(actual) = manager.observe(&intent.iface).await? {
            if actual.active || ![10, 20, 30].contains(&actual.state) {
                return Err("NetworkManager has an active AP operation");
            }
            if let Some(nm) = &intent.nm {
                if actual.path != nm.path {
                    return Err("NetworkManager device changed before removal");
                }
            }
        } else if intent.nm.is_some() {
            return Err("Recorded NetworkManager AP disappeared");
        }
    }
    Ok(())
}
