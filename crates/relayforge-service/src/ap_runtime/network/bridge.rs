//! Existing administrator bridge; only the journalled AP child is mutated.
use super::*;
use relayforge_core::config::Config;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Scope {
    boot: String,
    device: u64,
    inode: u64,
}
fn scope() -> Result<Scope> {
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .map_err(|_| "Cannot inspect bridge boot identity")?;
    let ns =
        fs::metadata("/proc/self/ns/net").map_err(|_| "Cannot inspect bridge network namespace")?;
    Ok(Scope {
        boot: boot.trim().into(),
        device: ns.dev(),
        inode: ns.ino(),
    })
}
fn interruption(p: &Platform, point: &str) -> Result<()> {
    if let Some(root) = &p.fixture {
        if let Some(bytes) = Directory::open(root)?.read("bridge-control.json", true, LIMIT)? {
            let control: serde_json::Value =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid bridge fault control")?;
            if control["after"].as_str() == Some(point) {
                return Err("Simulated bridge transaction interruption");
            }
        }
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq)]
struct Link {
    identity: Interface,
    kind: String,
    master: Option<String>,
    up: bool,
    fixed_mac: bool,
}
fn decode_link(name: &str, identity: Interface, text: &str) -> Result<Link> {
    interface_name(name)?;
    if text.len() > relayforge_core::radio::MAX_CAPTURE {
        return Err("Oversized bridge link observation");
    }
    let rows: serde_json::Value =
        serde_json::from_str(&text).map_err(|_| "Invalid bridge link observation")?;
    let rows = rows
        .as_array()
        .filter(|v| v.len() == 1)
        .ok_or("Ambiguous bridge link observation")?;
    let row = &rows[0];
    if row["ifname"].as_str() != Some(name)
        || row["ifindex"].as_u64() != Some(u64::from(identity.ifindex))
        || !row["address"]
            .as_str()
            .is_some_and(|m| m.eq_ignore_ascii_case(&identity.mac))
    {
        return Err("Bridge link changed during observation");
    }
    let master = row
        .get("master")
        .map(|v| {
            v.as_str()
                .map(str::to_owned)
                .ok_or("Invalid bridge master observation")
        })
        .transpose()?;
    if let Some(master) = &master {
        interface_name(master)?;
    }
    let kind = row
        .get("linkinfo")
        .and_then(|v| v.get("info_kind"))
        .map(|v| v.as_str().map(str::to_owned).ok_or("Invalid link kind"))
        .transpose()?
        .unwrap_or_default();
    let flags = row["flags"].as_array().ok_or("Missing bridge link flags")?;
    Ok(Link {
        identity,
        kind,
        master,
        up: flags.iter().any(|v| v.as_str() == Some("UP")),
        fixed_mac: false,
    })
}
impl Kernel<'_> {
    async fn link(&self, name: &str) -> Result<Option<Link>> {
        interface_name(name)?;
        if self.p.fixture.is_some() {
            return Ok(self.simulation()?.1.interfaces.get(name).map(|d| Link {
                identity: d.identity.clone(),
                kind: d.kind.clone(),
                master: d.master.clone(),
                up: d.up,
                fixed_mac: d.address_assignment == Some(3),
            }));
        }
        let Some(identity) = self.p.interface(name)? else {
            return Ok(None);
        };
        let text = self
            .command("ip", &["-j", "-d", "link", "show", "dev", name])
            .await?;
        let mut link = decode_link(name, identity, &text)?;
        if link.kind == "bridge" {
            link.fixed_mac = fs::read_to_string(self.p.net.join(name).join("addr_assign_type"))
                .map_err(|_| "Cannot inspect bridge MAC policy")?
                .trim()
                == "3";
        }
        Ok(Some(link))
    }

    async fn master(
        &self,
        name: &str,
        expected: &Interface,
        before: Option<&str>,
        target: Option<&str>,
    ) -> Result<()> {
        let current = self.inspect(name).await?.ok_or("Bridge AP disappeared")?;
        if current.identity != *expected
            || current.connected
            || current.master.as_deref() != before
            || current.kind != "AP"
            || current.addresses.iter().any(|v| nonlocal(v))
        {
            return Err("Bridge AP changed before membership mutation");
        }
        let mut args = vec!["link", "set", "dev", name];
        if let Some(target) = target {
            interface_name(target)?;
            args.extend(["master", target]);
        } else {
            args.push("nomaster");
        }
        if self.p.fixture.is_some() {
            return self.simulate(
                std::iter::once("ip".into())
                    .chain(args.iter().map(|s| s.to_string()))
                    .collect(),
                |s| {
                    s.interfaces
                        .get_mut(name)
                        .ok_or("Bridge AP missing")?
                        .master = target.map(str::to_owned);
                    Ok(())
                },
            );
        }
        self.command("ip", &args).await.map(|_| ())
    }
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
enum Phase {
    Prepared,
    ChildOwned,
    AttachRequested,
    Attached,
    DetachRequested,
    Clean,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    version: u32,
    owner: Identity,
    marker: String,
    scope: Scope,
    bridge: String,
    bridge_identity: Interface,
    parent: String,
    parent_identity: Interface,
    phy: u32,
    child: Option<(String, Interface)>,
    attach_authorized: bool,
    phase: Phase,
}
pub(in crate::ap_runtime) fn validate_config(config: &Config) -> Result<()> {
    if config["BACKEND"] != "create_ap"
        || config["SHARE_METHOD"] != "bridge"
        || config["NO_VIRT"] != "0"
        || config["WIFI_IFACE"] == config["INTERNET_IFACE"]
    {
        return Err(
            "Existing-bridge mode requires a separate administrator bridge and an owned virtual AP",
        );
    }
    Ok(())
}
pub(in crate::ap_runtime) async fn preflight(p: &Platform, config: &Config) -> Result<()> {
    validate_config(config)?;
    let kernel = Kernel { p };
    let bridge = kernel.link(&config["INTERNET_IFACE"]).await?.ok_or(
        "An existing administrator bridge is required; select an existing bridge as the upstream",
    )?;
    if bridge.kind != "bridge" || !bridge.up || bridge.master.is_some() {
        return Err("The selected upstream must be an active, independent Linux bridge");
    }
    if !bridge.fixed_mac {
        return Err("Use an administrator bridge with a fixed MAC address before attaching an AP");
    }
    p.interface(&config["WIFI_IFACE"])?
        .ok_or("Bridge radio parent is missing")?;
    kernel.phy(&config["WIFI_IFACE"]).await?;
    Ok(())
}
impl Intent {
    fn validate(&self, data: &Snapshot) -> Result<()> {
        interface_name(&self.bridge)?;
        interface_name(&self.parent)?;
        valid_mac(&self.bridge_identity.mac)?;
        valid_mac(&self.parent_identity.mac)?;
        if self.version != 1
            || self.owner != data.owner
            || self.marker != data.fields["DAEMON_PIDFILE"]
            || self.bridge != data.fields["INTERNET_IFACE"]
            || self.bridge != data.fields["BRIDGE_IFACE"]
            || data.fields["SHARE_METHOD"] != "bridge"
            || data.fields["NO_VIRT"] != "0"
            || data.fields["BRIDGE_CREATED"] != "0"
            || self.parent == self.bridge
            || self.bridge_identity.ifindex == 0
            || self.parent_identity.ifindex == 0
            || data.interfaces.get(&self.bridge) != Some(&self.bridge_identity)
            || data.interfaces.get(&self.parent) != Some(&self.parent_identity)
        {
            return Err("Invalid or changed bridge ownership plan");
        }
        if let Some((name, identity)) = &self.child {
            interface_name(name)?;
            valid_mac(&identity.mac)?;
            if name != &data.fields["VWIFI_IFACE"]
                || name == &self.parent
                || name == &self.bridge
                || identity.ifindex == 0
                || data.interfaces.get(name) != Some(identity)
                || !identity
                    .mac
                    .eq_ignore_ascii_case(&data.fields["NEW_MACADDR"])
            {
                return Err("Bridge child ownership changed");
            }
        }
        if self.child.is_none()
            && !matches!(
                self.phase,
                Phase::Prepared | Phase::DetachRequested | Phase::Clean
            )
        {
            return Err("Bridge phase has no owned child");
        }
        if matches!(self.phase, Phase::AttachRequested | Phase::Attached) && !self.attach_authorized
        {
            return Err("Bridge attachment was never authorized");
        }
        Ok(())
    }
    async fn guard(&self, p: &Platform, data: &Snapshot) -> Result<()> {
        self.validate(data)?;
        let kernel = Kernel { p };
        if self.scope != scope()?
            || p.interface(&self.parent)?.as_ref() != Some(&self.parent_identity)
            || kernel.phy(&self.parent).await? != self.phy
        {
            return Err("Bridge parent or namespace changed; evidence retained");
        }
        let bridge = kernel
            .link(&self.bridge)
            .await?
            .ok_or("Administrator bridge disappeared; evidence retained")?;
        if bridge.identity != self.bridge_identity
            || bridge.kind != "bridge"
            || !bridge.up
            || bridge.master.is_some()
            || !bridge.fixed_mac
        {
            return Err("Administrator bridge changed; evidence retained");
        }
        Ok(())
    }
    async fn child(&self, p: &Platform) -> Result<Option<Netdev>> {
        let Some((name, identity)) = &self.child else {
            return Ok(None);
        };
        let kernel = Kernel { p };
        let Some(current) = kernel.inspect(name).await? else {
            return Ok(None);
        };
        if current.identity != *identity
            || current.connected
            || current.kind != "AP"
            || kernel.phy(name).await? != self.phy
            || current.addresses.iter().any(|v| nonlocal(v))
            || current.master.as_ref().is_some_and(|m| m != &self.bridge)
        {
            return Err("Owned bridge AP changed; evidence retained");
        }
        Ok(Some(current))
    }
}
fn read(dir: &Directory, data: &Snapshot) -> Result<(Intent, Vec<u8>)> {
    let bytes = dir
        .read("bridge.json", true, LIMIT)?
        .ok_or("Missing existing-bridge authorization journal")?;
    let intent: Intent = serde_json::from_slice(&bytes).map_err(|_| "Invalid bridge journal")?;
    intent.validate(data)?;
    Ok((intent, bytes))
}
pub(in crate::ap_runtime) async fn prepare(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
    config: &Config,
) -> Result<()> {
    validate_config(config)?;
    if data.owner.current()?.is_none() {
        return Err("Bridge owner exited before planning");
    }
    if dir.read("bridge.json", true, LIMIT)?.is_some() {
        return Err("Bridge plan already exists");
    }
    let kernel = Kernel { p };
    let parent = &config["WIFI_IFACE"];
    let intent = Intent {
        version: 1,
        owner: data.owner.clone(),
        marker: data.fields["DAEMON_PIDFILE"].clone(),
        scope: scope()?,
        bridge: config["INTERNET_IFACE"].clone(),
        bridge_identity: p
            .interface(&config["INTERNET_IFACE"])?
            .ok_or("Existing administrator bridge is required")?,
        parent: parent.clone(),
        parent_identity: p.interface(parent)?.ok_or("Bridge radio parent missing")?,
        phy: kernel.phy(parent).await?,
        child: None,
        attach_authorized: false,
        phase: Phase::Prepared,
    };
    intent.guard(p, data).await?;
    dir.write("bridge.json", &intent, None)?;
    interruption(p, "Prepared")
}
pub(in crate::ap_runtime) async fn guard(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    let (intent, _) = read(dir, data)?;
    if matches!(intent.phase, Phase::DetachRequested | Phase::Clean) {
        return Err("Finish bridge cleanup before retrying");
    }
    intent.guard(p, data).await
}
pub(in crate::ap_runtime) async fn configure(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    let (mut intent, mut bytes) = read(dir, data)?;
    intent.guard(p, data).await?;
    if data.owner.current()?.is_none()
        || matches!(intent.phase, Phase::DetachRequested | Phase::Clean)
    {
        return Err("Bridge setup no longer authorized");
    }
    let kernel = Kernel { p };
    if intent.child.is_none() {
        let name = &data.fields["VWIFI_IFACE"];
        let current = kernel
            .inspect(name)
            .await?
            .ok_or("Owned bridge AP missing")?;
        if current.master.is_some() {
            return Err("Bridge child already has a foreign master");
        }
        intent.child = Some((name.clone(), current.identity));
        intent.phase = Phase::ChildOwned;
        intent.validate(data)?;
        intent.child(p).await?;
        dir.write("bridge.json", &intent, Some(&bytes))?;
        interruption(p, "ChildOwned")?;
        bytes = dir.read("bridge.json", true, LIMIT)?.unwrap();
    }
    let (name, identity) = intent.child.as_ref().unwrap().clone();
    let current = intent.child(p).await?.ok_or("Owned bridge AP missing")?;
    if current.master.is_some() && intent.phase == Phase::ChildOwned {
        return Err("Unrequested bridge attachment; evidence retained");
    }
    if current.master.is_none() {
        if intent.phase == Phase::Attached {
            return Err("Bridge membership changed externally; evidence retained");
        }
        if current.up {
            kernel.edit(&name, &identity, "down", "").await?;
        }
        intent.guard(p, data).await?;
        intent.phase = Phase::AttachRequested;
        intent.attach_authorized = true;
        dir.write("bridge.json", &intent, Some(&bytes))?;
        interruption(p, "AttachRequested")?;
        bytes = dir.read("bridge.json", true, LIMIT)?.unwrap();
        kernel
            .master(&name, &identity, None, Some(&intent.bridge))
            .await?;
    }
    if intent
        .child(p)
        .await?
        .as_ref()
        .and_then(|v| v.master.as_deref())
        != Some(intent.bridge.as_str())
    {
        return Err("Bridge attachment unconfirmed; evidence retained");
    }
    intent.guard(p, data).await?;
    intent.phase = Phase::Attached;
    dir.write("bridge.json", &intent, Some(&bytes))?;
    interruption(p, "Attached")
}
pub(in crate::ap_runtime) async fn ready(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    let (intent, _) = read(dir, data)?;
    intent.guard(p, data).await?;
    if intent.phase != Phase::Attached
        || intent
            .child(p)
            .await?
            .as_ref()
            .and_then(|v| v.master.as_deref())
            != Some(intent.bridge.as_str())
    {
        return Err("Owned bridge attachment is not ready");
    }
    Ok(())
}
pub(in crate::ap_runtime) async fn begin_stop(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    if data.fields["SHARE_METHOD"] != "bridge" {
        return Ok(());
    }
    let (mut intent, bytes) = read(dir, data)?;
    if intent.phase == Phase::Attached && intent.child(p).await?.is_some_and(|v| v.master.is_none())
    {
        return Err("Bridge membership changed before stop; evidence retained");
    }
    if !matches!(intent.phase, Phase::DetachRequested | Phase::Clean) {
        intent.phase = Phase::DetachRequested;
        dir.write("bridge.json", &intent, Some(&bytes))?;
        interruption(p, "DetachRequested")?;
    }
    Ok(())
}
pub(in crate::ap_runtime) async fn remove(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    begin_stop(p, dir, data).await?;
    let (mut intent, mut bytes) = read(dir, data)?;
    intent.guard(p, data).await?;
    let kernel = Kernel { p };
    if intent.phase == Phase::Clean {
        return if intent.child(p).await?.is_none() {
            Ok(())
        } else {
            Err("AP reappeared after bridge cleanup")
        };
    }
    if intent.child.is_none() && !data.fields["VWIFI_IFACE"].is_empty() {
        let name = &data.fields["VWIFI_IFACE"];
        if let Some(current) = kernel.inspect(name).await? {
            if current.master.is_some() {
                return Err("Unjournalled bridge attachment retained");
            }
            intent.child = Some((name.clone(), current.identity));
            intent.validate(data)?;
            intent.child(p).await?;
            dir.write("bridge.json", &intent, Some(&bytes))?;
            bytes = dir.read("bridge.json", true, LIMIT)?.unwrap();
        }
    }
    if let Some(current) = intent.child(p).await? {
        if intent.phase == Phase::Clean {
            return Err("AP reappeared after bridge cleanup");
        }
        let (name, identity) = intent.child.as_ref().unwrap();
        if current.master.is_some() {
            if !intent.attach_authorized {
                return Err("Unrequested bridge attachment retained");
            }
            intent.guard(p, data).await?;
            kernel
                .master(name, identity, Some(&intent.bridge), None)
                .await?;
            interruption(p, "detached")?;
        }
        intent.guard(p, data).await?;
        let current = intent
            .child(p)
            .await?
            .ok_or("Bridge AP disappeared during cleanup")?;
        if current.master.is_some() {
            return Err("Bridge detachment unconfirmed");
        }
        kernel.edit(name, identity, "delete", "").await?;
        interruption(p, "deleted")?;
        if kernel.inspect(name).await?.is_some() {
            return Err("Bridge AP deletion unconfirmed");
        }
    }
    intent.phase = Phase::Clean;
    dir.write("bridge.json", &intent, Some(&bytes))?;
    interruption(p, "Clean")
}
pub(in crate::ap_runtime) async fn clean(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
) -> Result<()> {
    let (intent, _) = read(dir, data)?;
    intent.guard(p, data).await?;
    if intent.phase != Phase::Clean || intent.child(p).await?.is_some() {
        return Err("Bridge cleanup is unconfirmed");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn identity() -> Interface {
        Interface {
            ifindex: 9,
            mac: "02:00:00:00:00:09".into(),
        }
    }
    fn row() -> serde_json::Value {
        serde_json::json!([{"ifname":"brtest0","ifindex":9,"address":"02:00:00:00:00:09",
            "flags":["BROADCAST","MULTICAST","UP","LOWER_UP"],"operstate":"UP",
            "linkinfo":{"info_kind":"bridge","info_data":{"stp_state":0,"vlan_filtering":0}}}])
    }
    #[test]
    fn iproute_link_shape_preserves_identity_kind_master_and_flags() {
        let mut rows = row();
        let decoded = decode_link("brtest0", identity(), &rows.to_string()).unwrap();
        assert_eq!(decoded.identity, identity());
        assert_eq!(decoded.kind, "bridge");
        assert!(decoded.up);
        assert!(!decoded.fixed_mac);
        assert_eq!(decoded.master, None);
        rows[0]["master"] = serde_json::json!("vrftest0");
        rows[0]["flags"] = serde_json::json!(["BROADCAST"]);
        let decoded = decode_link("brtest0", identity(), &rows.to_string()).unwrap();
        assert!(!decoded.up);
        assert_eq!(decoded.master.as_deref(), Some("vrftest0"));
    }
    #[test]
    fn ambiguous_replaced_and_malformed_link_observations_are_refused() {
        for (field, value) in [
            ("ifindex", serde_json::json!(10)),
            ("ifname", serde_json::json!("foreign0")),
            ("address", serde_json::json!("02:00:00:00:00:99")),
            ("flags", serde_json::Value::Null),
            ("master", serde_json::json!(9)),
        ] {
            let mut rows = row();
            rows[0][field] = value;
            assert!(decode_link("brtest0", identity(), &rows.to_string()).is_err());
        }
        for text in ["[]", "{}", "[{},{}]", "malformed"] {
            assert!(decode_link("brtest0", identity(), text).is_err());
        }
        assert!(decode_link(
            "brtest0",
            identity(),
            &" ".repeat(relayforge_core::radio::MAX_CAPTURE + 1)
        )
        .is_err());
    }
}
