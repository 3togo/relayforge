//! Explicit BSSID-only reassociation. Never edits profiles or requests secrets.
//! Ambiguous calls and foreign changes retain evidence instead of guessing cleanup.
use crate::{
    networkmanager::{field, Bus, Identity, Platform, Settings},
    nm_journal::{Journal, Record, Result},
};
use relayforge_core::{
    config::Config,
    radio::{band, Snapshot},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, path::Path, sync::Arc, time::Duration};
use tokio::{
    sync::{watch, Mutex},
    time::{sleep, timeout},
};
use zbus::zvariant::{OwnedObjectPath, OwnedValue};
const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const WIFI: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
const AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
const UNCERTAIN: &str = "Band-switch recovery is unconfirmed; ownership journal retained. Inspect the upstream before retrying";

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn fingerprint(settings: &Settings) -> Result<String> {
    let mut data =
        serde_json::to_value(settings).map_err(|_| "Cannot fingerprint upstream profile")?;
    // NM updates last-used/seen metadata itself. All other non-secret settings
    // must match; no GetSecrets/Update/Save calls are made by this controller.
    if let Some(connection) = data.get_mut("connection").and_then(Value::as_object_mut) {
        connection.remove("timestamp");
    }
    if let Some(wifi) = data
        .get_mut("802-11-wireless")
        .and_then(Value::as_object_mut)
    {
        wifi.remove("seen-bssids");
    }
    Ok(hash(
        &serde_json::to_vec(&data).map_err(|_| "Cannot fingerprint upstream profile")?,
    ))
}
fn mac(value: &str) -> bool {
    value.len() == 17
        && value.split(':').count() == 6
        && value
            .split(':')
            .all(|v| v.len() == 2 && v.bytes().all(|b| b.is_ascii_hexdigit()))
        && u8::from_str_radix(&value[..2], 16).is_ok_and(|v| v & 1 == 0)
        && value != "00:00:00:00:00:00"
}
fn object(value: &str, kind: &str) -> bool {
    value
        .strip_prefix(&format!("{ROOT}/{kind}/"))
        .is_some_and(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Association {
    path: String,
    bssid: String,
    frequency: u32,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "stage", deny_unknown_fields)]
enum Stage {
    Prepared,
    Switching { active: String },
    RestorePrepared,
    Restoring { active: String },
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    version: u32,
    bus_id: String,
    owner: String,
    interface: String,
    identity: Identity,
    device: String,
    profile: String,
    uuid: String,
    settings_hash: String,
    ssid_hash: String,
    original: Association,
    target: Association,
    phase: Stage,
}
impl Record for Intent {
    fn validate_record(&self) -> Result<()> {
        relayforge_core::config::parse_patch(&json!({"WIFI_IFACE":self.interface}).to_string())?;
        let digest = |s: &str| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit());
        if self.version != 1
            || self.identity.ifindex == 0
            || !mac(&self.identity.mac)
            || self.bus_id.len() != 32
            || !self.bus_id.bytes().all(|b| b.is_ascii_hexdigit())
            || !self.owner.starts_with(':')
            || self.owner.len() > 64
            || !object(&self.device, "Devices")
            || !object(&self.profile, "Settings")
            || self.uuid.len() != 36
            || !digest(&self.settings_hash)
            || !digest(&self.ssid_hash)
            || [&self.original, &self.target].iter().any(|a| {
                !object(&a.path, "AccessPoint") || !mac(&a.bssid) || band(a.frequency).is_empty()
            })
        {
            return Err("Invalid band-switch ownership record");
        }
        match &self.phase {
            Stage::Switching { active } | Stage::Restoring { active }
                if !object(active, "ActiveConnection") =>
            {
                Err("Invalid band-switch activation identity")
            }
            _ => Ok(()),
        }
    }
}
struct Candidate {
    association: Association,
    ssid: Vec<u8>,
    strength: u8,
    seen: i32,
}
async fn access_point(bus: &Bus, path: &str) -> Result<Candidate> {
    if !object(path, "AccessPoint") {
        return Err("Invalid scanned access point identity");
    }
    let ssid: Vec<u8> = bus.property(path, AP, "Ssid").await?;
    if ssid.len() > 32 {
        return Err("Oversized scanned SSID");
    }
    let bssid: String = bus.property(path, AP, "HwAddress").await?;
    let frequency: u32 = bus.property(path, AP, "Frequency").await?;
    let strength: u8 = bus.property(path, AP, "Strength").await?;
    let seen: i32 = bus.property(path, AP, "LastSeen").await?;
    let mode: u32 = bus.property(path, AP, "Mode").await?;
    if !mac(&bssid) || strength > 100 || mode != 2 {
        return Err("Invalid scanned access point properties");
    }
    Ok(Candidate {
        association: Association {
            path: path.into(),
            bssid: bssid.to_lowercase(),
            frequency,
        },
        ssid,
        strength,
        seen,
    })
}
fn boottime() -> Result<i64> {
    let mut clock = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock points to initialized writable storage.
    if unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut clock) } != 0 {
        return Err("Cannot inspect scan freshness");
    }
    Ok(clock.tv_sec as i64)
}
fn choose(
    mut candidates: Vec<Candidate>,
    snapshot: &Snapshot,
    ssid: &[u8],
    target: &str,
    now: i64,
) -> Result<Association> {
    let caps = &snapshot.capabilities;
    if !matches!(target, "2.4" | "5") || !caps.ap_sta_concurrent {
        return Err("Concurrent band switching is unavailable");
    }
    candidates.retain(|c| {
        c.ssid == ssid
            && band(c.association.frequency) == target
            && c.seen >= 0
            && i64::from(c.seen) <= now
            && now - i64::from(c.seen) <= 45
            && caps.ap_channels.get(target).is_some_and(|channels| {
                channels
                    .iter()
                    .any(|ch| ch.frequency == c.association.frequency)
            })
    });
    candidates.sort_by(|a, b| {
        b.strength
            .cmp(&a.strength)
            .then(a.association.bssid.cmp(&b.association.bssid))
    });
    candidates
        .into_iter()
        .next()
        .map(|c| c.association)
        .ok_or("No fresh same-network access point permits hotspot sharing on the requested band")
}
async fn provider(bus: &Bus) -> Result<String> {
    let proxy = zbus::fdo::DBusProxy::new(&bus.connection)
        .await
        .map_err(|_| UNCERTAIN)?;
    let owner = timeout(
        Duration::from_secs(3),
        proxy.get_name_owner(NM.try_into().unwrap()),
    )
    .await
    .map_err(|_| UNCERTAIN)?
    .map_err(|_| UNCERTAIN)?;
    if owner.as_str() != bus.owner {
        return Err("NetworkManager owner changed; band-switch journal retained");
    }
    timeout(Duration::from_secs(3), proxy.get_id())
        .await
        .map_err(|_| UNCERTAIN)?
        .map(|id| id.to_string())
        .map_err(|_| UNCERTAIN)
}
async fn device_active(bus: &Bus, device: &str) -> Result<OwnedObjectPath> {
    bus.property(device, DEVICE, "ActiveConnection").await
}
async fn guard(bus: &Bus, platform: &Platform, intent: &Intent) -> Result<()> {
    if provider(bus).await? != intent.bus_id
        || bus.owner != intent.owner
        || platform.identity(bus, &intent.interface).await?.as_ref() != Some(&intent.identity)
        || bus.device(&intent.interface).await?.as_str() != intent.device
    {
        return Err("Upstream provider/device identity changed; band-switch journal retained");
    }
    let settings = bus.settings(&intent.profile).await?;
    if field(&settings, "connection", "uuid") != Some(&intent.uuid)
        || fingerprint(&settings)? != intent.settings_hash
    {
        return Err("Upstream profile changed; band-switch journal retained");
    }
    Ok(())
}
async fn verified(
    bus: &Bus,
    platform: &Platform,
    intent: &Intent,
    active: &str,
    expected: &Association,
) -> Result<bool> {
    guard(bus, platform, intent).await?;
    if device_active(bus, &intent.device).await?.as_str() != active {
        return Err("Upstream activation changed externally; band-switch journal retained");
    }
    let connection: OwnedObjectPath = bus.property(active, ACTIVE, "Connection").await?;
    let uuid: String = bus.property(active, ACTIVE, "Uuid").await?;
    let state: u32 = bus.property(active, ACTIVE, "State").await?;
    if connection.as_str() != intent.profile || uuid != intent.uuid {
        return Err(UNCERTAIN);
    }
    if state == 4 {
        return Err("Requested Wi-Fi association failed");
    }
    if state != 2 {
        return Ok(false);
    }
    let ap: OwnedObjectPath = bus
        .property(&intent.device, WIFI, "ActiveAccessPoint")
        .await?;
    let observed = access_point(bus, ap.as_str()).await?;
    let radio = platform.inspect(bus, &intent.interface).await?;
    let caps = &radio.capabilities;
    Ok(observed.association.bssid == expected.bssid
        && observed.association.frequency == expected.frequency
        && hash(&observed.ssid) == intent.ssid_hash
        && caps.current_sta_bssid.eq_ignore_ascii_case(&expected.bssid)
        && caps.current_sta_frequency == expected.frequency
        && (expected.bssid == intent.original.bssid
            && expected.frequency == intent.original.frequency
            || caps.current_sta_ap_allowed)
        && hash(caps.current_sta_ssid.as_bytes()) == intent.ssid_hash)
}
async fn wait_association(
    bus: &Bus,
    platform: &Platform,
    intent: &Intent,
    active: &str,
    expected: &Association,
    cancel: Option<&watch::Receiver<u64>>,
    revision: u64,
) -> Result<()> {
    timeout(
        Duration::from_secs(10),
        wait_association_inner(bus, platform, intent, active, expected, cancel, revision),
    )
    .await
    .map_err(|_| "Wi-Fi association verification timed out")?
}
async fn wait_association_inner(
    bus: &Bus,
    platform: &Platform,
    intent: &Intent,
    active: &str,
    expected: &Association,
    cancel: Option<&watch::Receiver<u64>>,
    revision: u64,
) -> Result<()> {
    for _ in 0..20 {
        if cancel.is_some_and(|c| *c.borrow() != revision) {
            return Err("Band switch cancelled");
        }
        if verified(bus, platform, intent, active, expected).await? {
            if cancel.is_some_and(|c| *c.borrow() != revision) {
                return Err("Band switch cancelled");
            }
            return Ok(());
        }
        sleep(Duration::from_millis(100)).await;
    }
    Err("Requested Wi-Fi association was not confirmed")
}
async fn activate(bus: &Bus, intent: &Intent, ap: &Association) -> Result<OwnedObjectPath> {
    let profile = OwnedObjectPath::try_from(intent.profile.clone()).map_err(|_| UNCERTAIN)?;
    let device = OwnedObjectPath::try_from(intent.device.clone()).map_err(|_| UNCERTAIN)?;
    let point = OwnedObjectPath::try_from(ap.path.clone()).map_err(|_| UNCERTAIN)?;
    bus.call(ROOT, NM, "ActivateConnection", &(profile, device, point))
        .await
}

pub(crate) struct Backend {
    platform: Platform,
    journal: Mutex<Journal<Intent>>,
}
impl Backend {
    pub(crate) fn open(path: &Path, fixture: bool) -> Result<Arc<Self>> {
        use std::os::unix::fs::DirBuilderExt;
        let directory = path.join("band-switch");
        if !directory.exists() {
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&directory)
                .map_err(|_| "Cannot create band-switch ownership directory")?;
        }
        Ok(Arc::new(Self {
            platform: Platform { fixture },
            journal: Mutex::new(Journal::open_named(
                &directory,
                unsafe { libc::geteuid() },
                c"band-switch.json",
            )?),
        }))
    }
    pub(crate) async fn pending(&self) -> bool {
        self.journal
            .lock()
            .await
            .load()
            .map_or(true, |r| r.is_some())
    }
    async fn restore(
        &self,
        bus: &Bus,
        journal: &mut Journal<Intent>,
        mut intent: Intent,
    ) -> Result<()> {
        match &intent.phase {
            Stage::Prepared | Stage::RestorePrepared => return Err(UNCERTAIN),
            Stage::Restoring { active } => {
                wait_association(
                    bus,
                    &self.platform,
                    &intent,
                    active,
                    &intent.original,
                    None,
                    0,
                )
                .await?;
            }
            Stage::Switching { active } => {
                guard(bus, &self.platform, &intent).await?;
                if device_active(bus, &intent.device).await?.as_str() != active {
                    return Err(UNCERTAIN);
                }
                // The active object returned by our request must still reference
                // the recorded profile; a foreign activation is never adopted.
                let profile: OwnedObjectPath = bus.property(active, ACTIVE, "Connection").await?;
                let uuid: String = bus.property(active, ACTIVE, "Uuid").await?;
                if profile.as_str() != intent.profile || uuid != intent.uuid {
                    return Err(UNCERTAIN);
                }
                let current_ap: OwnedObjectPath = bus
                    .property(&intent.device, WIFI, "ActiveAccessPoint")
                    .await?;
                let current = access_point(bus, current_ap.as_str()).await?;
                if current.association.bssid != intent.target.bssid
                    || current.association.frequency != intent.target.frequency
                    || hash(&current.ssid) != intent.ssid_hash
                {
                    return Err(UNCERTAIN);
                }
                let old = access_point(bus, &intent.original.path).await?;
                if old.association.bssid != intent.original.bssid
                    || old.association.frequency != intent.original.frequency
                    || hash(&old.ssid) != intent.ssid_hash
                {
                    return Err(UNCERTAIN);
                }
                intent.phase = Stage::RestorePrepared;
                journal.write(&intent)?;
                let active = activate(bus, &intent, &intent.original).await?;
                intent.phase = Stage::Restoring {
                    active: active.to_string(),
                };
                journal.write(&intent)?;
                wait_association(
                    bus,
                    &self.platform,
                    &intent,
                    active.as_str(),
                    &intent.original,
                    None,
                    0,
                )
                .await?;
            }
        }
        journal.clear()
    }
    pub(crate) async fn recover(&self) -> Result<()> {
        let mut journal = self.journal.lock().await;
        let Some(intent) = journal.load()? else {
            return Ok(());
        };
        let bus = Bus::open(self.platform.fixture).await?;
        let result = self.restore(&bus, &mut journal, intent).await;
        bus.close().await;
        result
    }
    async fn prepare(
        &self,
        bus: &Bus,
        config: &Config,
        target: &str,
        cancel: &mut watch::Receiver<u64>,
        revision: u64,
    ) -> Result<(Intent, String)> {
        let iface = &config["WIFI_IFACE"];
        let bus_id = provider(bus).await?;
        let device = bus.device(iface).await?;
        let identity = self
            .platform
            .identity(bus, iface)
            .await?
            .ok_or("Wi-Fi adapter disappeared")?;
        let original_active = device_active(bus, device.as_str()).await?;
        let state: u32 = bus
            .property(original_active.as_str(), ACTIVE, "State")
            .await?;
        let profile: OwnedObjectPath = bus
            .property(original_active.as_str(), ACTIVE, "Connection")
            .await?;
        let settings = bus.settings(profile.as_str()).await?;
        if state != 2
            || field(&settings, "connection", "type") != Some("802-11-wireless")
            || field(&settings, "802-11-wireless", "mode").is_some_and(|m| m != "infrastructure")
        {
            return Err("Band switching requires a connected infrastructure Wi-Fi profile");
        }
        if field(&settings, "802-11-wireless", "band").is_some_and(|b| !b.is_empty())
            || settings.get("802-11-wireless").is_some_and(|s| {
                s.contains_key("bssid")
                    || s.contains_key("band")
                        && field(&settings, "802-11-wireless", "band").is_none()
                    || s.get("channel").is_some_and(|v| {
                        v.try_clone()
                            .ok()
                            .and_then(|v| u32::try_from(v).ok())
                            .unwrap_or(1)
                            != 0
                    })
            })
        {
            return Err("Saved Wi-Fi profile pins a band, BSSID or channel; edit it explicitly before switching");
        }
        let uuid = field(&settings, "connection", "uuid")
            .ok_or("Upstream profile UUID is missing")?
            .to_owned();
        let ap: OwnedObjectPath = bus
            .property(device.as_str(), WIFI, "ActiveAccessPoint")
            .await?;
        let original = access_point(bus, ap.as_str()).await?;
        if original.ssid.is_empty() {
            return Err("Original Wi-Fi SSID is unavailable");
        }
        let profile_ssid = settings
            .get("802-11-wireless")
            .and_then(|s| s.get("ssid"))
            .and_then(|v| v.try_clone().ok())
            .and_then(|v| Vec::<u8>::try_from(v).ok());
        if profile_ssid.as_ref() != Some(&original.ssid) {
            return Err("Active Wi-Fi SSID does not match its saved profile");
        }
        let previous: i64 = bus.property(device.as_str(), WIFI, "LastScan").await?;
        let _: () = bus
            .call(
                device.as_str(),
                WIFI,
                "RequestScan",
                &(HashMap::<String, OwnedValue>::new(),),
            )
            .await?;
        let mut scanned = false;
        for _ in 0..30 {
            if *cancel.borrow() != revision {
                return Err("Band switch cancelled before activation");
            }
            let current: i64 = bus.property(device.as_str(), WIFI, "LastScan").await?;
            if current >= 0 && current > previous {
                scanned = true;
                break;
            }
            sleep(Duration::from_millis(100)).await;
        }
        if !scanned {
            return Err("Wi-Fi scan completion was not confirmed");
        }
        let snapshot = self.platform.inspect(bus, iface).await?;
        let paths: Vec<OwnedObjectPath> = bus
            .call(device.as_str(), WIFI, "GetAllAccessPoints", &())
            .await?;
        if paths.len() > 128 {
            return Err("Wi-Fi scan exceeds the bounded candidate limit");
        }
        let mut candidates = Vec::new();
        for path in paths {
            candidates.push(access_point(bus, path.as_str()).await?);
        }
        let selected = if band(original.association.frequency) == target
            && snapshot.capabilities.current_sta_ap_allowed
        {
            original.association.clone()
        } else {
            choose(candidates, &snapshot, &original.ssid, target, boottime()?)?
        };
        let intent = Intent {
            version: 1,
            bus_id,
            owner: bus.owner.clone(),
            interface: iface.clone(),
            identity,
            device: device.to_string(),
            profile: profile.to_string(),
            uuid,
            settings_hash: fingerprint(&settings)?,
            ssid_hash: hash(&original.ssid),
            original: original.association,
            target: selected,
            phase: Stage::Prepared,
        };
        intent.validate_record()?;
        // A scan must not make a stale baseline eligible for a reconnect.
        if !verified(
            bus,
            &self.platform,
            &intent,
            original_active.as_str(),
            &intent.original,
        )
        .await?
        {
            return Err("Original Wi-Fi association changed while scanning");
        }
        Ok((intent, original_active.to_string()))
    }
    pub(crate) async fn switch(
        &self,
        config: &Config,
        target: &str,
        mut cancel: watch::Receiver<u64>,
    ) -> Value {
        let revision = *cancel.borrow();
        if cancel.has_changed().unwrap_or(true) {
            return json!({"success":false,"error":"Band switch cancelled while queued"});
        }
        let mut journal = self.journal.lock().await;
        if !matches!(journal.load(), Ok(None)) {
            return json!({"success":false,"error":UNCERTAIN});
        }
        let result = async {
            let bus = Bus::open(self.platform.fixture).await?;
            let result = async {
                let (mut intent, original_active) = timeout(
                    Duration::from_secs(12),
                    self.prepare(&bus, config, target, &mut cancel, revision),
                )
                .await
                .map_err(|_| "Band-switch preparation timed out")??;
                if *cancel.borrow() != revision {
                    return Err("Band switch cancelled before activation");
                }
                if band(intent.original.frequency) == target
                    && intent.original.bssid == intent.target.bssid
                {
                    return Ok(());
                }
                guard(&bus, &self.platform, &intent).await?;
                if !verified(
                    &bus,
                    &self.platform,
                    &intent,
                    &original_active,
                    &intent.original,
                )
                .await?
                {
                    return Err("Original Wi-Fi association changed before activation");
                }
                // Write ahead: an activation timeout/crash cannot be treated as
                // evidence that the upstream was untouched.
                journal.write(&intent)?;
                let active = activate(&bus, &intent, &intent.target).await?;
                if !object(active.as_str(), "ActiveConnection")
                    || active.as_str() == original_active
                {
                    return Err(UNCERTAIN);
                }
                intent.phase = Stage::Switching {
                    active: active.to_string(),
                };
                journal.write(&intent)?;
                let outcome = wait_association(
                    &bus,
                    &self.platform,
                    &intent,
                    active.as_str(),
                    &intent.target,
                    Some(&cancel),
                    revision,
                )
                .await;
                match outcome {
                    Ok(()) => journal.clear(),
                    Err(error) => {
                        self.restore(&bus, &mut journal, intent)
                            .await
                            .map_err(|_| UNCERTAIN)?;
                        Err(error)
                    }
                }
            }
            .await;
            bus.close().await;
            result
        }
        .await;
        match result {
            Ok(()) => json!({"success":true}),
            Err(error) => {
                json!({"success":false,"error":if matches!(journal.load(), Ok(None)) { error } else { UNCERTAIN }})
            }
        }
    }
}
