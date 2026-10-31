//! Read-only radio policy and a private baseline across native startup phases.
use super::*;
use relayforge_core::radio::Capture;

#[derive(Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Baseline {
    version: u32,
    owner: Identity,
    parent: Interface,
    phy: u32,
    module: String,
    config_hash: String,
    station_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    regulatory_hash: Option<String>,
}

fn digest(value: &impl Serialize) -> Result<String> {
    let bytes = serde_json::to_vec(value).map_err(|_| "Cannot bind radio baseline")?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

async fn inspect(p: &Platform, iface: &str) -> Result<(relayforge_core::radio::Snapshot, String)> {
    if let Some(root) = &p.fixture {
        let dir = Directory::open(root)?;
        let bytes = dir
            .read("live-radio.json", true, LIMIT)?
            .ok_or("Missing simulated radio")?;
        let capture: Capture =
            serde_json::from_slice(&bytes).map_err(|_| "Invalid simulated radio")?;
        if capture.wifi_iface != iface {
            return Err("Simulated radio adapter mismatch");
        }
        let module = dir
            .read("radio-module", true, 128)?
            .ok_or("Missing simulated radio driver")?;
        let module = String::from_utf8(module).map_err(|_| "Invalid simulated driver encoding")?;
        return Ok((relayforge_core::radio::Snapshot::parse(&capture)?, module));
    }
    Ok((
        radio::Source::System.inspect(iface).await?,
        module(p, iface)?,
    ))
}

pub(super) fn module(p: &Platform, iface: &str) -> Result<String> {
    interface_name(iface)?;
    if let Some(root) = &p.fixture {
        let bytes = Directory::open(root)?
            .read("radio-module", true, 128)?
            .ok_or("Missing simulated radio driver")?;
        return String::from_utf8(bytes).map_err(|_| "Invalid simulated driver encoding");
    }
    let driver = p.net.join(iface).join("device/driver");
    let link = match fs::read_link(driver.join("module")) {
        Ok(path) => path,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::read_link(driver).map_err(|_| "Cannot inspect radio driver")?
        }
        Err(_) => return Err("Cannot inspect radio module"),
    };
    let module = link
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or("Invalid radio module")?
        .to_owned();
    Ok(module)
}

pub(super) async fn check(p: &Platform, dir: &Directory, prepare: bool) -> Result<()> {
    let _lock = dir.transaction_lock(c".radio.lock").await?;
    let (data, _) = snapshot(p, dir)?;
    data.interfaces(p)?;
    if data.owner.current()?.is_none() {
        return Err("Radio owner exited");
    }
    let config = daemons::frozen(p, &data)?;
    if config["SHARE_METHOD"] == "bridge" {
        network::bridge::guard(p, dir, &data).await?;
    }
    let iface = &config["WIFI_IFACE"];
    let parent = p.interface(iface)?.ok_or("Radio parent disappeared")?;
    if data.interfaces.get(iface) != Some(&parent) {
        return Err("Radio parent identity changed");
    }
    let (radio, module) = inspect(p, iface).await?;
    relayforge_core::ap_policy::driver(&module, &config)?;
    if !["managed", "AP"].contains(&radio.interface_type.as_str())
        || config["NO_VIRT"] == "0" && radio.interface_type != "managed"
    {
        return Err("Unsupported native parent radio mode");
    }
    let plan = radio.preflight(&config)?;
    if plan.switch_upstream
        || plan.band != config["FREQ_BAND"]
        || plan.channel.map(|v| v.to_string()).as_ref() != Some(&config["CHANNEL"])
    {
        return Err("Frozen radio plan no longer matches the permitted upstream channel");
    }
    let regulatory_hash = environment::regulatory(p, &config, radio.phy).await?;
    environment::firewall(p, &config["SHARE_METHOD"]).await?;
    let caps = radio.capabilities;
    let baseline = Baseline {
        version: 1,
        owner: data.owner,
        parent,
        phy: radio.phy,
        module,
        regulatory_hash,
        config_hash: digest(&config)?,
        station_hash: digest(&(
            caps.current_sta_ssid,
            caps.current_sta_bssid,
            caps.current_sta_frequency,
        ))?,
    };
    match dir.read("radio.json", true, LIMIT)? {
        Some(bytes) => {
            let previous: Baseline =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid radio baseline")?;
            if previous != baseline {
                return Err("Radio baseline changed; stop before retrying");
            }
            Ok(())
        }
        None if prepare => dir.write("radio.json", &baseline, None),
        None => Err("Check the native radio plan before configuring daemons"),
    }
}
