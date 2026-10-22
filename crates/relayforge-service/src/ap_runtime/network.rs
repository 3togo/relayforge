//! Owned AP initialization/teardown and journalled forwarding leases.
mod devices;
use super::*;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Netdev {
    identity: Interface,
    kind: String,
    up: bool,
    connected: bool,
    addresses: Vec<String>,
}
#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Simulation {
    interfaces: BTreeMap<String, Netdev>,
    phys: BTreeMap<String, u32>,
    routes: Vec<String>,
    nm: Option<devices::SimManager>,
    nm_bus: bool,
    global: u8,
    forwarding: BTreeMap<String, u8>,
    calls: Vec<Vec<String>>,
    mutations: u32,
    fail: bool,
    fail_at: Option<u32>,
    lose_at: Option<u32>,
}
struct Kernel<'a> {
    p: &'a Platform,
}
impl Kernel<'_> {
    fn simulation(&self) -> Result<(Directory, Simulation, Vec<u8>)> {
        let dir = Directory::open(self.p.fixture.as_ref().ok_or("Not a simulated kernel")?)?;
        let bytes = dir
            .read("network-state.json", true, LIMIT)?
            .ok_or("Missing simulated network state")?;
        let state =
            serde_json::from_slice(&bytes).map_err(|_| "Invalid simulated network state")?;
        Ok((dir, state, bytes))
    }
    fn simulate(
        &self,
        call: Vec<String>,
        edit: impl FnOnce(&mut Simulation) -> Result<()>,
    ) -> Result<()> {
        let (dir, mut s, bytes) = self.simulation()?;
        if s.calls.len() > 256 {
            return Err("Simulated network call limit exceeded");
        }
        s.calls.push(call);
        s.mutations += 1;
        let failed = s.fail || s.fail_at == Some(s.mutations);
        if !failed {
            edit(&mut s)?;
        }
        dir.write("network-state.json", &s, Some(&bytes))?;
        if failed {
            return Err("Simulated network mutation failed");
        }
        for (name, dev) in &s.interfaces {
            interface_name(name)?;
            let path = self.p.net.join(name);
            fs::create_dir_all(&path).map_err(|_| "Cannot update simulated interface")?;
            fs::write(path.join("ifindex"), format!("{}\n", dev.identity.ifindex))
                .map_err(|_| "Cannot update simulated index")?;
            fs::write(path.join("address"), format!("{}\n", dev.identity.mac))
                .map_err(|_| "Cannot update simulated MAC")?;
        }
        if s.lose_at == Some(s.mutations) {
            return Err("Simulated network reply lost");
        }
        Ok(())
    }
    async fn subnet(&self, data: &Snapshot, exclude: Option<&str>) -> Result<()> {
        let routes = if self.p.fixture.is_some() {
            let s = self.simulation()?.1;
            let mut routes = s.routes;
            for (name, device) in s.interfaces {
                if exclude == Some(name.as_str()) {
                    continue;
                }
                routes.extend(device.addresses.into_iter().filter(|v| {
                    v.split('/')
                        .next()
                        .is_some_and(|v| v.parse::<std::net::Ipv4Addr>().is_ok())
                }));
            }
            routes
        } else {
            let text = self
                .command("ip", &["-j", "-4", "route", "show", "scope", "link"])
                .await?;
            let routes: Vec<serde_json::Value> =
                serde_json::from_str(&text).map_err(|_| "Cannot inspect connected routes")?;
            let mut result = Vec::new();
            for route in routes {
                let dev = route["dev"]
                    .as_str()
                    .ok_or("Missing connected route interface")?;
                if exclude == Some(dev) {
                    continue;
                }
                result.push(
                    route["dst"]
                        .as_str()
                        .ok_or("Invalid connected route")?
                        .to_owned(),
                );
            }
            result
        };
        relayforge_core::subnet::check(&data.fields["GATEWAY"], &routes)
    }
    async fn phy(&self, name: &str) -> Result<u32> {
        interface_name(name)?;
        if self.p.fixture.is_some() {
            return self
                .simulation()?
                .1
                .phys
                .get(name)
                .copied()
                .ok_or("Missing simulated radio identity");
        }
        let text = self.command("iw", &["dev", name, "info"]).await?;
        Ok(relayforge_core::radio::device(&text, name)?.0)
    }
    async fn macs(&self) -> Result<Vec<String>> {
        if self.p.fixture.is_some() {
            return Ok(self
                .simulation()?
                .1
                .interfaces
                .values()
                .map(|v| v.identity.mac.clone())
                .collect());
        }
        let mut result = Vec::new();
        for entry in fs::read_dir(&self.p.net).map_err(|_| "Cannot enumerate MAC addresses")? {
            let entry = entry.map_err(|_| "Cannot inspect MAC address")?;
            let mac = fs::read_to_string(entry.path().join("address"))
                .map_err(|_| "Cannot read MAC address")?;
            result.push(mac.trim().to_lowercase());
            if result.len() > 128 {
                return Err("Too many interfaces");
            }
        }
        for line in self.command("iw", &["dev"]).await?.lines() {
            if let Some(mac) = line.trim().strip_prefix("addr ") {
                valid_mac(mac)?;
                result.push(mac.to_lowercase());
            }
        }
        Ok(result)
    }
    async fn create(&self, parent: &str, name: &str, mac: &str, phy: u32) -> Result<()> {
        interface_name(parent)?;
        interface_name(name)?;
        valid_mac(mac)?;
        if self.p.interface(name)?.is_some() {
            return Err("AP name already exists");
        }
        if self.p.fixture.is_some() {
            return self.simulate(
                vec![
                    "iw".into(),
                    "dev".into(),
                    parent.into(),
                    "interface".into(),
                    "add".into(),
                    name.into(),
                    "type".into(),
                    "__ap".into(),
                    "addr".into(),
                    mac.into(),
                ],
                |s| {
                    if s.interfaces.contains_key(name) {
                        return Err("AP name collision");
                    }
                    let index = s
                        .interfaces
                        .values()
                        .map(|v| v.identity.ifindex)
                        .max()
                        .unwrap_or(0)
                        .checked_add(1)
                        .ok_or("Interface index exhausted")?;
                    s.interfaces.insert(
                        name.into(),
                        Netdev {
                            identity: Interface {
                                ifindex: index,
                                mac: mac.into(),
                            },
                            kind: "AP".into(),
                            up: false,
                            connected: false,
                            addresses: Vec::new(),
                        },
                    );
                    s.phys.insert(name.into(), phy);
                    s.forwarding
                        .insert(name.into(), *s.forwarding.get("default").unwrap_or(&0));
                    if let Some(nm) = &mut s.nm {
                        if !nm.omit_created {
                            nm.devices.insert(
                                name.into(),
                                devices::SimDevice {
                                    path: format!(
                                        "/org/freedesktop/NetworkManager/Devices/{index}"
                                    ),
                                    managed: true,
                                    state: 30,
                                    active: false,
                                },
                            );
                        }
                    }
                    Ok(())
                },
            );
        }
        self.command(
            "iw",
            &[
                "dev",
                parent,
                "interface",
                "add",
                name,
                "type",
                "__ap",
                "addr",
                mac,
            ],
        )
        .await
        .map(|_| ())
    }
    async fn command(&self, utility: &str, args: &[&str]) -> Result<String> {
        let path = ["/usr/sbin", "/usr/bin", "/sbin", "/bin"]
            .iter()
            .map(|p| Path::new(p).join(utility))
            .find(|p| p.is_file())
            .ok_or("Required network utility unavailable")?;
        let env = BTreeMap::from([
            ("PATH".into(), "/usr/sbin:/usr/bin:/sbin:/bin".into()),
            ("LC_ALL".into(), "C".into()),
        ]);
        radio::command_env(&path, args, Duration::from_secs(3), Some(&env))
            .await
            .map_err(|_| "Network utility failed or timed out")
    }
    async fn inspect(&self, name: &str) -> Result<Option<Netdev>> {
        interface_name(name)?;
        if self.p.fixture.is_some() {
            return Ok(self.simulation()?.1.interfaces.get(name).cloned());
        }
        let Some(identity) = self.p.interface(name)? else {
            return Ok(None);
        };
        let text = self.command("iw", &["dev", name, "info"]).await?;
        let (_, kind) = relayforge_core::radio::device(&text, name)?;
        let link = self.command("iw", &["dev", name, "link"]).await?;
        let connected = if link.trim_start().starts_with("Connected to ") {
            true
        } else if link.trim() == "Not connected." {
            false
        } else {
            return Err("Unknown AP station-link state");
        };
        let text = self
            .command("ip", &["-j", "address", "show", "dev", name])
            .await?;
        let rows: serde_json::Value =
            serde_json::from_str(&text).map_err(|_| "Invalid interface address observation")?;
        let rows = rows
            .as_array()
            .filter(|r| r.len() == 1)
            .ok_or("Ambiguous interface address observation")?;
        let row = &rows[0];
        if row["ifindex"].as_u64() != Some(u64::from(identity.ifindex))
            || row["ifname"].as_str() != Some(name)
        {
            return Err("Interface changed during observation");
        }
        let flags = row["flags"].as_array().ok_or("Missing interface flags")?;
        let mut addresses = Vec::new();
        for addr in row["addr_info"]
            .as_array()
            .ok_or("Missing interface address data")?
        {
            let local = addr["local"].as_str().ok_or("Missing interface address")?;
            let prefix = addr["prefixlen"].as_u64().ok_or("Missing address prefix")?;
            let ip: std::net::IpAddr = local
                .parse()
                .map_err(|_| "Invalid observed interface address")?;
            if prefix > if ip.is_ipv4() { 32 } else { 128 } {
                return Err("Invalid observed address prefix");
            }
            addresses.push(format!("{ip}/{prefix}"));
        }
        Ok(Some(Netdev {
            identity,
            kind,
            connected,
            up: flags.iter().any(|f| f.as_str() == Some("UP")),
            addresses,
        }))
    }
    async fn edit(
        &self,
        name: &str,
        expected: &Interface,
        operation: &str,
        address: &str,
    ) -> Result<()> {
        let current = self
            .inspect(name)
            .await?
            .ok_or("AP interface disappeared")?;
        if current.identity != *expected || current.connected {
            return Err("AP identity or station connection changed");
        }
        let args: Vec<&str> = match operation {
            "down" => vec!["link", "set", "dev", name, "down"],
            "up" => vec!["link", "set", "dev", name, "up"],
            "AP" => vec!["dev", name, "set", "type", "__ap"],
            "managed" => vec!["dev", name, "set", "type", "managed"],
            "add" => vec!["address", "add", address, "dev", name],
            "del" => vec!["address", "del", address, "dev", name],
            "delete" => vec!["dev", name, "del"],
            _ => return Err("Unsupported interface mutation"),
        };
        let utility = if matches!(operation, "AP" | "managed" | "delete") {
            "iw"
        } else {
            "ip"
        };
        if self.p.fixture.is_some() {
            let deleting = operation == "delete";
            let result = self.simulate(
                std::iter::once(utility.to_owned())
                    .chain(args.iter().map(|s| s.to_string()))
                    .collect(),
                |s| {
                    let dev = s.interfaces.get_mut(name).ok_or("Missing simulated AP")?;
                    match operation {
                        "down" => dev.up = false,
                        "up" => dev.up = true,
                        "AP" | "managed" => dev.kind = operation.into(),
                        "add" => {
                            if !dev.addresses.iter().any(|v| v == address) {
                                dev.addresses.push(address.into());
                            }
                        }
                        "del" => dev.addresses.retain(|v| v != address),
                        "delete" => {}
                        _ => unreachable!(),
                    }
                    if deleting {
                        s.interfaces.remove(name);
                        s.forwarding.remove(name);
                        s.phys.remove(name);
                        if let Some(nm) = &mut s.nm {
                            nm.devices.remove(name);
                        }
                    }
                    Ok(())
                },
            );
            if deleting && !self.simulation()?.1.interfaces.contains_key(name) {
                let path = self.p.net.join(name);
                if path.exists() {
                    fs::remove_dir_all(path).map_err(|_| "Cannot remove simulated AP metadata")?;
                }
            }
            return result;
        }
        self.command(utility, &args).await.map(|_| ())
    }
    fn flags(&self) -> Result<BTreeMap<String, (Option<Interface>, u8)>> {
        if self.p.fixture.is_some() {
            let s = self.simulation()?.1;
            return s
                .forwarding
                .iter()
                .map(|(name, value)| {
                    let id = if name == "default" || name == "all" {
                        None
                    } else {
                        Some(
                            s.interfaces
                                .get(name)
                                .ok_or("Unknown simulated forwarding device")?
                                .identity
                                .clone(),
                        )
                    };
                    Ok((name.clone(), (id, *value)))
                })
                .collect();
        }
        let mut flags = BTreeMap::new();
        for entry in
            fs::read_dir(&self.p.net).map_err(|_| "Cannot enumerate forwarding interfaces")?
        {
            let name = entry
                .map_err(|_| "Cannot enumerate forwarding interfaces")?
                .file_name()
                .into_string()
                .map_err(|_| "Invalid forwarding interface")?;
            interface_name(&name)?;
            flags.insert(
                name.clone(),
                (self.p.interface(&name)?, self.read_flag(&name)?),
            );
            if flags.len() > 128 {
                return Err("Too many forwarding interfaces");
            }
        }
        for name in ["all", "default"] {
            flags.insert(name.into(), (None, self.read_flag(name)?));
        }
        Ok(flags)
    }
    fn read_flag(&self, name: &str) -> Result<u8> {
        if self.p.fixture.is_some() {
            let s = self.simulation()?.1;
            return if name == "global" {
                Ok(s.global)
            } else {
                s.forwarding
                    .get(name)
                    .copied()
                    .ok_or("Missing simulated forwarding flag")
            };
        }
        let path = flag_path(name)?;
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "Cannot read forwarding sysctl")?;
        let mut bytes = Vec::new();
        file.take(8)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read forwarding value")?;
        match std::str::from_utf8(&bytes).map(str::trim) {
            Ok("0") => Ok(0),
            Ok("1") => Ok(1),
            _ => Err("Invalid forwarding sysctl value"),
        }
    }
    fn write_flag(&self, name: &str, value: u8) -> Result<()> {
        if value > 1 {
            return Err("Invalid forwarding intent");
        }
        if self.p.fixture.is_some() {
            return self.simulate(vec!["sysctl".into(), name.into(), value.to_string()], |s| {
                if name == "global" {
                    s.global = value;
                    for v in s.forwarding.values_mut() {
                        *v = value;
                    }
                } else {
                    *s.forwarding
                        .get_mut(name)
                        .ok_or("Missing simulated forwarding flag")? = value;
                }
                Ok(())
            });
        }
        let mut file = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(flag_path(name)?)
            .map_err(|_| "Cannot write forwarding sysctl")?;
        file.write_all(if value == 1 { b"1\n" } else { b"0\n" })
            .map_err(|_| "Cannot commit forwarding sysctl")?;
        if self.read_flag(name)? != value {
            return Err("Forwarding sysctl write unconfirmed");
        }
        Ok(())
    }
}
fn flag_path(name: &str) -> Result<PathBuf> {
    if name == "global" {
        return Ok("/proc/sys/net/ipv4/ip_forward".into());
    }
    interface_name(name)?;
    Ok(Path::new("/proc/sys/net/ipv4/conf")
        .join(name)
        .join("forwarding"))
}
fn nonlocal(address: &str) -> bool {
    address
        .split('/')
        .next()
        .and_then(|s| s.parse::<std::net::IpAddr>().ok())
        .is_none_or(|ip| match ip {
            std::net::IpAddr::V6(v) => !v.is_unicast_link_local(),
            std::net::IpAddr::V4(_) => true,
        })
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct InterfaceIntent {
    version: u32,
    marker: String,
    owner: Identity,
    iface: String,
    virtual_ap: bool,
    gateway: String,
    baseline: Netdev,
    restored: bool,
}
impl InterfaceIntent {
    fn validate(&self, data: &Snapshot) -> Result<()> {
        if self.version != 1
            || self.marker != data.fields["DAEMON_PIDFILE"]
            || self.owner != data.owner
            || self.iface != data.fields["WIFI_IFACE"]
            || self.virtual_ap != (data.fields["NO_VIRT"] == "0")
            || self.gateway != format!("{}/24", data.fields["GATEWAY"])
            || !["AP", "managed"].contains(&self.baseline.kind.as_str())
            || self.baseline.connected
            || self.baseline.identity.ifindex == 0
            || self.baseline.addresses.iter().any(|a| nonlocal(a))
        {
            return Err("Invalid or changed interface intent");
        }
        valid_mac(&self.baseline.identity.mac)?;
        Ok(())
    }
    fn check_current(&self, current: &Netdev) -> Result<()> {
        if current.identity != self.baseline.identity
            || current.connected
            || ![self.baseline.kind.as_str(), "AP"].contains(&current.kind.as_str())
            || current
                .addresses
                .iter()
                .any(|a| nonlocal(a) && a != &self.gateway)
        {
            return Err("AP state changed outside the interface transaction");
        }
        Ok(())
    }
}
async fn configure(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    if data.owner.current()?.is_none() {
        return Err("Interface owner exited");
    }
    let f = &data.fields;
    let iface = &f["WIFI_IFACE"];
    if f["SHARE_METHOD"] == "bridge"
        || iface == &f["INTERNET_IFACE"]
        || (f["NO_VIRT"] == "0" && f["VWIFI_IFACE"] != *iface)
    {
        return Err("Interface transaction cannot target the upstream or bridge");
    }
    check_subnet(p, dir, data).await?;
    let kernel = Kernel { p };
    let current = kernel.inspect(iface).await?.ok_or("AP interface missing")?;
    let bytes = dir.read("interface.json", true, LIMIT)?;
    let intent: InterfaceIntent = if let Some(bytes) = &bytes {
        serde_json::from_slice(bytes).map_err(|_| "Invalid interface journal")?
    } else {
        if f["NO_VIRT"] == "1"
            && !f["NEW_MACADDR"].is_empty()
            && !current.identity.mac.eq_ignore_ascii_case(&f["NEW_MACADDR"])
        {
            return Err("Physical MAC changes are unavailable in native setup");
        }
        if current.connected
            || current.addresses.iter().any(|a| nonlocal(a))
            || !["AP", "managed"].contains(&current.kind.as_str())
        {
            return Err("Dedicated AP is connected or already configured");
        }
        if data.interfaces.get(iface) != Some(&current.identity)
            || (f["NO_VIRT"] == "0"
                && !current.identity.mac.eq_ignore_ascii_case(&f["NEW_MACADDR"]))
        {
            return Err("AP identity differs from the planned interface");
        }
        InterfaceIntent {
            version: 1,
            marker: f["DAEMON_PIDFILE"].clone(),
            owner: data.owner.clone(),
            iface: iface.clone(),
            virtual_ap: f["NO_VIRT"] == "0",
            gateway: format!("{}/24", f["GATEWAY"]),
            baseline: current,
            restored: false,
        }
    };
    intent.validate(data)?;
    if intent.restored {
        return Err("Finish cleanup before configuring this instance again");
    }
    intent.check_current(&kernel.inspect(iface).await?.ok_or("AP missing")?)?;
    dir.write("interface.json", &intent, bytes.as_deref())?;
    for operation in ["down", "AP", "add"] {
        if data.owner.current()?.is_none() {
            return Err("Interface owner exited during configuration");
        }
        let observed = kernel.inspect(iface).await?.ok_or("AP missing")?;
        intent.check_current(&observed)?;
        let needed = match operation {
            "down" => observed.up,
            "AP" => observed.kind != "AP",
            _ => !observed.addresses.contains(&intent.gateway),
        };
        if needed {
            kernel
                .edit(iface, &intent.baseline.identity, operation, &intent.gateway)
                .await?;
        }
    }
    let result = kernel
        .inspect(iface)
        .await?
        .ok_or("Configured AP missing")?;
    intent.check_current(&result)?;
    if result.up || result.kind != "AP" || !result.addresses.contains(&intent.gateway) {
        return Err("AP configuration unconfirmed");
    }
    Ok(())
}
async fn remove(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    let bytes = dir.read("interface.json", true, LIMIT)?;
    let iface = &data.fields["WIFI_IFACE"];
    let kernel = Kernel { p };
    let intent: Option<InterfaceIntent> = bytes
        .as_deref()
        .map(serde_json::from_slice)
        .transpose()
        .map_err(|_| "Invalid interface journal")?;
    if let Some(intent) = &intent {
        intent.validate(data)?;
    }
    if intent.is_none()
        && data.fields["NO_VIRT"] == "0"
        && iface != &data.fields["VWIFI_IFACE"]
        && (data.fields["VWIFI_IFACE"].is_empty()
            || p.interface(&data.fields["VWIFI_IFACE"])?.is_none())
    {
        return Ok(());
    }
    let current = kernel.inspect(iface).await?;
    if let Some(intent) = &intent {
        let baseline_returned = !intent.virtual_ap
            && current.as_ref().is_some_and(|v| {
                v.identity == intent.baseline.identity
                    && v.kind == intent.baseline.kind
                    && v.up == intent.baseline.up
                    && !v.addresses.contains(&intent.gateway)
            });
        if intent.restored || current.is_none() || baseline_returned {
            if intent.virtual_ap && current.is_some() {
                return Err("A new interface occupies the removed AP name");
            }
            let mut done: InterfaceIntent = serde_json::from_slice(bytes.as_deref().unwrap())
                .map_err(|_| "Invalid interface journal")?;
            done.restored = true;
            return dir.write("interface.json", &done, bytes.as_deref());
        }
    }
    let Some(current) = current else {
        return Ok(());
    };
    if let Some(intent) = &intent {
        intent.check_current(&current)?;
    } else {
        if data.fields["NO_VIRT"] != "0" || data.fields["VWIFI_IFACE"].is_empty() {
            return if data.fields["PHYSICAL_CONFIGURED"] == "0" {
                Ok(())
            } else {
                Err("Physical AP has no native restoration journal")
            };
        }
        if iface != &data.fields["VWIFI_IFACE"]
            || data.interfaces.get(iface) != Some(&current.identity)
            || current.kind != "AP"
            || current.connected
        {
            return Err("Unjournalled AP identity changed");
        }
    }
    if data.fields["NO_VIRT"] == "0" {
        kernel.edit(iface, &current.identity, "delete", "").await?;
        if kernel.inspect(iface).await?.is_some() {
            return Err("Virtual AP removal unconfirmed");
        }
    } else {
        let intent = intent
            .as_ref()
            .ok_or("Missing physical AP restoration journal")?;
        for operation in [
            "down",
            "del",
            intent.baseline.kind.as_str(),
            if intent.baseline.up { "up" } else { "down" },
        ] {
            let current = kernel
                .inspect(iface)
                .await?
                .ok_or("Dedicated AP disappeared")?;
            intent.check_current(&current)?;
            let needed = match operation {
                "down" => current.up,
                "up" => !current.up,
                "del" => current.addresses.contains(&intent.gateway),
                _ => current.kind != operation,
            };
            if needed {
                kernel
                    .edit(iface, &intent.baseline.identity, operation, &intent.gateway)
                    .await?;
            }
        }
        let observed = kernel
            .inspect(iface)
            .await?
            .ok_or("Restored AP disappeared")?;
        intent.check_current(&observed)?;
        if observed.up != intent.baseline.up
            || observed.kind != intent.baseline.kind
            || observed.addresses.contains(&intent.gateway)
        {
            return Err("Dedicated AP restoration unconfirmed");
        }
    }
    if let Some(bytes) = bytes {
        let mut done: InterfaceIntent =
            serde_json::from_slice(&bytes).map_err(|_| "Invalid interface journal")?;
        done.restored = true;
        dir.write("interface.json", &done, Some(&bytes))?;
    }
    Ok(())
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Flag {
    identity: Option<Interface>,
    baseline: u8,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Lease {
    marker: String,
    owner: Identity,
    uplink: String,
    phase: String,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Forwarding {
    version: u32,
    restoring: bool,
    global: u8,
    flags: BTreeMap<String, Flag>,
    owners: BTreeMap<String, Lease>,
}
impl Forwarding {
    fn validate(&self) -> Result<()> {
        if self.version != 1 || self.global > 1 || self.flags.len() > 130 || self.owners.len() > 32
        {
            return Err("Invalid forwarding journal");
        }
        if self
            .flags
            .get("all")
            .map(|v| (v.identity.is_none(), v.baseline))
            != Some((true, self.global))
            || !self
                .flags
                .get("default")
                .is_some_and(|v| v.identity.is_none())
        {
            return Err("Missing forwarding defaults");
        }
        for (name, flag) in &self.flags {
            interface_name(name)?;
            if name == "global" || flag.baseline > 1 {
                return Err("Invalid forwarding baseline");
            }
            if let Some(id) = &flag.identity {
                if id.ifindex == 0 {
                    return Err("Invalid forwarding interface index");
                }
                valid_mac(&id.mac)?;
            } else if !["all", "default"].contains(&name.as_str()) {
                return Err("Missing forwarding interface identity");
            }
        }
        if self.restoring && self.owners.values().any(|v| v.phase != "releasing") {
            return Err("Invalid shared forwarding restoration phase");
        }
        for (name, lease) in &self.owners {
            if !instance(name)
                || !self.flags.contains_key(&lease.uplink)
                || !["preparing", "active", "releasing"].contains(&lease.phase.as_str())
            {
                return Err("Invalid forwarding lease");
            }
            lease.owner.validate()?;
        }
        Ok(())
    }
    fn lease(&self, name: &str, data: &Snapshot) -> Result<()> {
        if let Some(lease) = self.owners.get(name) {
            if lease.owner != data.owner
                || lease.marker != data.fields["DAEMON_PIDFILE"]
                || lease.uplink != data.fields["INTERNET_IFACE"]
            {
                return Err("Forwarding lease ownership changed");
            }
        }
        Ok(())
    }
}
fn legacy(p: &Platform) -> Result<()> {
    let path = p.root.join("create_ap.common.conf");
    if !path.exists() {
        return Ok(());
    }
    let dir = p.open(&path, true)?;
    for entry in fs::read_dir(&dir.path).map_err(|_| "Cannot inspect shared legacy state")? {
        let name = entry
            .map_err(|_| "Cannot inspect shared legacy state")?
            .file_name()
            .to_string_lossy()
            .into_owned();
        if name == "ip_forward"
            || name == "bridge-nf-call-iptables"
            || name.ends_with("_forwarding")
        {
            return Err("Stop the previous adapter before migrating its shared forwarding backups");
        }
    }
    Ok(())
}
fn foreign(p: &Platform, state: &Forwarding, current: &str) -> Result<()> {
    for entry in fs::read_dir(&p.root).map_err(|_| "Cannot inspect forwarding participants")? {
        let name = entry
            .map_err(|_| "Cannot inspect forwarding participant")?
            .file_name()
            .to_string_lossy()
            .into_owned();
        if instance(&name) && name != current && !state.owners.contains_key(&name) {
            return Err("Foreign or historical AP state blocks shared forwarding changes");
        }
    }
    Ok(())
}
fn check_flags(
    kernel: &Kernel<'_>,
    state: &Forwarding,
) -> Result<BTreeMap<String, (Option<Interface>, u8)>> {
    let current = kernel.flags()?;
    if current.values().any(|(_, v)| *v > 1) {
        return Err("Invalid current forwarding value");
    }
    let reset = state.restoring && state.global == 0 && kernel.read_flag("global")? == 0;
    for (name, flag) in &state.flags {
        if let Some((id, value)) = current.get(name) {
            if id != &flag.identity || (*value != 1 && *value != flag.baseline && !reset) {
                return Err("Forwarding interface identity or value changed");
            }
        }
    }
    Ok(current)
}
async fn forwarding(p: &Platform, dir: &Directory, data: &Snapshot, operation: &str) -> Result<()> {
    legacy(p)?;
    let root = Directory::open(&p.root)?;
    let _lock = root.transaction_lock(c".forwarding.lock").await?;
    let previous = root.read("forwarding.json", true, LIMIT)?;
    let name = dir.path.file_name().unwrap().to_str().unwrap();
    let kernel = Kernel { p };
    let mut state = if let Some(bytes) = &previous {
        serde_json::from_slice::<Forwarding>(bytes).map_err(|_| "Invalid forwarding state JSON")?
    } else {
        if operation != "forwarding-enable" {
            return Ok(());
        }
        let global = kernel.read_flag("global")?;
        let flags = kernel
            .flags()?
            .into_iter()
            .map(|(n, (identity, baseline))| (n, Flag { identity, baseline }))
            .collect();
        Forwarding {
            version: 1,
            restoring: false,
            global,
            flags,
            owners: BTreeMap::new(),
        }
    };
    state.validate()?;
    state.lease(name, data)?;
    if operation == "network-check" {
        return if state.owners.contains_key(name) {
            Err("Forwarding lease still needs restoration")
        } else {
            Ok(())
        };
    }
    foreign(p, &state, name)?;
    let mut expected = previous;
    if operation == "forwarding-enable" {
        if state.restoring {
            return Err("Finish shared forwarding restoration before enabling again");
        }
        if data.fields["SHARE_METHOD"] != "nat" {
            return Err("Forwarding is only required for NAT");
        }
        if data.owner.current()?.is_none() {
            return Err("Forwarding owner exited");
        }
        let uplink = &data.fields["INTERNET_IFACE"];
        let actual = p.interface(uplink)?.ok_or("Forwarding uplink missing")?;
        if data.interfaces.get(uplink) != Some(&actual) {
            return Err("Forwarding uplink identity changed");
        }
        let flags = check_flags(&kernel, &state)?;
        if state.owners.values().any(|v| v.phase == "active") && kernel.read_flag("global")? != 1 {
            return Err("Active forwarding state was changed externally");
        }
        for lease in state.owners.values().filter(|v| v.phase == "active") {
            if kernel.read_flag(&lease.uplink)? != 1 {
                return Err("Active uplink forwarding changed externally");
            }
        }
        if state
            .owners
            .get(name)
            .is_some_and(|v| v.phase == "releasing")
        {
            return Err("Finish forwarding restoration before enabling again");
        }
        for (name, (identity, baseline)) in flags {
            state
                .flags
                .entry(name)
                .or_insert(Flag { identity, baseline });
        }
        state.owners.insert(
            name.into(),
            Lease {
                marker: data.fields["DAEMON_PIDFILE"].clone(),
                owner: data.owner.clone(),
                uplink: uplink.clone(),
                phase: "preparing".into(),
            },
        );
        state.validate()?;
        root.write("forwarding.json", &state, expected.as_deref())?;
        expected = root.read("forwarding.json", true, LIMIT)?;
        if kernel.read_flag("global")? != 1 {
            kernel.write_flag("global", 1)?;
        }
        if data.owner.current()?.is_none() {
            return Err("Forwarding owner exited during activation");
        }
        check_flags(&kernel, &state)?;
        if kernel.read_flag(uplink)? != 1 {
            kernel.write_flag(uplink, 1)?;
        }
        state.owners.get_mut(name).unwrap().phase = "active".into();
        state.validate()?;
        root.write("forwarding.json", &state, expected.as_deref())?;
    } else {
        if !state.owners.contains_key(name) {
            return Ok(());
        }
        state.owners.get_mut(name).unwrap().phase = "releasing".into();
        let current = check_flags(&kernel, &state)?;
        for (name, (identity, baseline)) in &current {
            state.flags.entry(name.clone()).or_insert(Flag {
                identity: identity.clone(),
                baseline: *baseline,
            });
        }
        state.validate()?;
        root.write("forwarding.json", &state, expected.as_deref())?;
        expected = root.read("forwarding.json", true, LIMIT)?;
        let remaining: Vec<_> = state
            .owners
            .iter()
            .filter(|(key, v)| key.as_str() != name && v.phase != "releasing")
            .map(|(_, v)| v.uplink.clone())
            .collect();
        if remaining.is_empty() {
            state.restoring = true;
            state.validate()?;
            root.write("forwarding.json", &state, expected.as_deref())?;
            expected = root.read("forwarding.json", true, LIMIT)?;
            let global = kernel.read_flag("global")?;
            if global != 1 && global != state.global {
                return Err("Global forwarding was changed externally");
            }
            if global != state.global {
                kernel.write_flag("global", state.global)?;
            }
            for (flag, baseline) in &state.flags {
                if flag == "all" {
                    continue;
                }
                if current.contains_key(flag) && kernel.read_flag(flag)? != baseline.baseline {
                    if kernel.flags()?.get(flag).map(|v| &v.0) != Some(&baseline.identity) {
                        return Err("Forwarding interface changed during restoration");
                    }
                    kernel.write_flag(flag, baseline.baseline)?;
                }
            }
        } else {
            if kernel.read_flag("global")? != 1 {
                return Err("Other forwarding owners still require global forwarding");
            }
            let uplink = &data.fields["INTERNET_IFACE"];
            if !remaining.contains(uplink) && current.contains_key(uplink) {
                let baseline = state
                    .flags
                    .get(uplink)
                    .ok_or("Missing uplink forwarding baseline")?
                    .baseline;
                if kernel.read_flag(uplink)? != baseline {
                    kernel.write_flag(uplink, baseline)?;
                }
            }
        }
        state.owners.remove(name);
        if state.owners.is_empty() {
            root.remove(
                "forwarding.json",
                expected.as_deref().ok_or("Missing forwarding intent")?,
            )?;
        } else {
            state.validate()?;
            root.write("forwarding.json", &state, expected.as_deref())?;
        }
    }
    Ok(())
}
pub(super) async fn device_action(
    p: &Platform,
    dir: &Directory,
    operation: &str,
) -> Result<String> {
    devices::action(p, dir, operation).await
}
pub(super) async fn run(p: &Platform, dir: &Directory, operation: &str) -> Result<()> {
    let (mut data, bytes) = snapshot(p, dir)?;
    data.interfaces(p)?;
    match operation {
        "interface-configure" | "interface-remove" => {
            let _lock = dir.transaction_lock(c".network.lock").await?;
            if operation == "interface-configure" {
                devices::require_release(p, dir, &data).await?;
                configure(p, dir, &data).await
            } else {
                devices::reconcile(p, dir, &mut data, &bytes).await?;
                devices::permit_remove(p, dir, &data).await?;
                remove(p, dir, &data).await
            }
        }
        "forwarding-enable" | "forwarding-release" | "network-check" => {
            let _lock = dir.transaction_lock(c".network.lock").await?;
            if operation == "network-check" {
                devices::check(dir, &data)?;
                if let Some(bytes) = dir.read("interface.json", true, LIMIT)? {
                    let intent: InterfaceIntent =
                        serde_json::from_slice(&bytes).map_err(|_| "Invalid interface journal")?;
                    intent.validate(&data)?;
                    if !intent.restored
                        || (intent.virtual_ap && p.interface(&intent.iface)?.is_some())
                    {
                        return Err("Interface restoration is not confirmed");
                    }
                } else if data.fields["NO_VIRT"] == "1" && data.fields["PHYSICAL_CONFIGURED"] == "1"
                {
                    return Err("Physical AP restoration has no journal");
                }
            }
            forwarding(p, dir, &data, operation).await
        }
        _ => Err("Unknown network transaction"),
    }
}

pub(super) async fn daemon_permit(p: &Platform, dir: &Directory) -> Result<()> {
    let _lock = dir.transaction_lock(c".network.lock").await?;
    let (data, _) = snapshot(p, dir)?;
    data.interfaces(p)?;
    devices::require_release(p, dir, &data).await?;
    let bytes = dir
        .read("interface.json", true, LIMIT)?
        .ok_or("Configure the AP before launching daemons")?;
    let intent: InterfaceIntent =
        serde_json::from_slice(&bytes).map_err(|_| "Invalid interface setup proof")?;
    intent.validate(&data)?;
    let current = Kernel { p }
        .inspect(&intent.iface)
        .await?
        .ok_or("AP disappeared before daemon launch")?;
    intent.check_current(&current)?;
    if intent.restored || current.kind != "AP" || !current.addresses.contains(&intent.gateway) {
        return Err("AP setup is incomplete");
    }
    Ok(())
}

pub(super) async fn check_subnet(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<()> {
    let iface = &data.fields["WIFI_IFACE"];
    let kernel = Kernel { p };
    let mut exclude = None;
    if data.fields["NO_VIRT"] == "0"
        && !data.fields["VWIFI_IFACE"].is_empty()
        && data.fields["VWIFI_IFACE"] == *iface
    {
        if let Some(current) = kernel.inspect(iface).await? {
            if data.interfaces.get(iface) == Some(&current.identity) {
                exclude = Some(iface.as_str());
            }
        }
    } else if let Some(bytes) = dir.read("interface.json", true, LIMIT)? {
        let intent: InterfaceIntent =
            serde_json::from_slice(&bytes).map_err(|_| "Invalid interface journal")?;
        intent.validate(data)?;
        if let Some(current) = kernel.inspect(iface).await? {
            intent.check_current(&current)?;
            exclude = Some(iface.as_str());
        }
    }
    kernel.subnet(data, exclude).await
}
