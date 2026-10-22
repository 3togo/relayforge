//! Offline native payload transactions. Never executes an installed program or contacts a bus.
mod fs;
use crate::config_store::random_secret;
use fs::{Data, Root};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fs::File, path::Path};
type Result<T> = std::result::Result<T, &'static str>;
const LIMIT: usize = 64 * 1024 * 1024;
const STATE: &str = "var/lib/relayforge-deploy";
const RECORD: &str = "var/lib/relayforge-deploy/state.json";
const SERVICE: &str = "usr/lib/relayforge/relayforge-service";
const HELPER: &str = "usr/lib/relayforge/native/create_ap";
const NM_UNIT: &str = "usr/lib/systemd/system/relayforge-native.service";
const AP_UNIT: &str = "usr/lib/systemd/system/relayforge-create-ap.service";
const ACTIVATION: &str =
    "usr/share/dbus-1/system-services/io.github.erhanzeyrek.WifiHotspot.service";
const POLICY: &str = "usr/share/dbus-1/system.d/io.github.erhanzeyrek.WifiHotspot.conf";
const POLKIT: &str = "usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy";
const LICENSE: &str = "usr/share/doc/relayforge/LICENSE";
const PATHS: [&str; 8] = [
    SERVICE, HELPER, NM_UNIT, AP_UNIT, POLICY, POLKIT, LICENSE, ACTIVATION,
];
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn hex(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
#[derive(Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Entry {
    sha256: String,
    mode: u32,
    gid: u32,
    size: usize,
}
impl Entry {
    fn of(data: &Data) -> Self {
        Self {
            sha256: digest(&data.bytes),
            mode: data.mode,
            gid: data.gid,
            size: data.bytes.len(),
        }
    }
    fn validate(&self) -> Result<()> {
        if !hex(&self.sha256, 64)
            || self.size > LIMIT
            || self.mode & !0o777 != 0
            || self.mode & 0o022 != 0
        {
            return Err("Invalid saved deployment entry");
        }
        Ok(())
    }
}
type Files = BTreeMap<String, Option<Entry>>;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    id: String,
    files: Files,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    version: u32,
    root: (u64, u64),
    backend: String,
    current: Snapshot,
    previous: Option<Snapshot>,
    pending: Option<Snapshot>,
    // Old version-1 receipts default to forward recovery. Persist an abort before
    // restoring any files so a second interruption cannot reverse that decision.
    #[serde(default)]
    aborting: bool,
}
impl State {
    fn validate(&self, root: &Root) -> Result<()> {
        if self.version != 1
            || self.aborting && self.pending.is_none()
            || self.root != root.identity()?
            || !matches!(self.backend.as_str(), "networkmanager" | "create_ap")
        {
            return Err("Deployment receipt belongs to another image or schema");
        }
        for snap in std::iter::once(&self.current)
            .chain(self.previous.iter())
            .chain(self.pending.iter())
        {
            if !hex(&snap.id, 32)
                || snap.files.keys().map(String::as_str).collect::<Vec<_>>()
                    != slots().keys().map(String::as_str).collect::<Vec<_>>()
            {
                return Err("Invalid deployment snapshot paths");
            }
            for entry in snap.files.values().flatten() {
                entry.validate()?;
            }
        }
        Ok(())
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestEntry {
    sha256: String,
    mode: String,
}
fn slots() -> Files {
    PATHS.into_iter().map(|p| (p.to_owned(), None)).collect()
}
fn load(root: &Root) -> Result<Option<State>> {
    if let Some(dir) = root.dir(STATE, false)? {
        use std::os::unix::fs::MetadataExt;
        if dir
            .metadata()
            .map_err(|_| "Cannot inspect deployment state")?
            .mode()
            & 0o7777
            != 0o700
        {
            return Err("Deployment state must be private");
        }
    }
    let Some(data) = root.read(RECORD, 65536)? else {
        return Ok(None);
    };
    if data.mode != 0o600 {
        return Err("Deployment receipt must be private");
    }
    let state: State =
        serde_json::from_slice(&data.bytes).map_err(|_| "Invalid deployment receipt")?;
    state.validate(root)?;
    Ok(Some(state))
}
fn save(root: &Root, state: &State) -> Result<()> {
    state.validate(root)?;
    root.write(
        RECORD,
        &Data {
            bytes: serde_json::to_vec(state).map_err(|_| "Cannot encode deployment receipt")?,
            mode: 0o600,
            gid: unsafe { libc::getegid() },
        },
    )
}
fn capture(root: &Root) -> Result<(Files, BTreeMap<String, Vec<u8>>)> {
    let mut files = slots();
    let mut blobs = BTreeMap::new();
    for path in PATHS {
        if let Some(data) = root.read(path, LIMIT)? {
            let entry = Entry::of(&data);
            blobs.insert(entry.sha256.clone(), data.bytes);
            files.insert(path.into(), Some(entry));
        }
    }
    Ok((files, blobs))
}
struct Payload {
    backend: String,
    files: Files,
    blobs: BTreeMap<String, Vec<u8>>,
}
fn payload(path: &Path, uid: u32) -> Result<Payload> {
    let root = Root::open(path, uid)?;
    let manifest = root
        .read("manifest.json", 65536)?
        .ok_or("Payload manifest is missing")?;
    let entries: BTreeMap<String, ManifestEntry> =
        serde_json::from_slice(&manifest.bytes).map_err(|_| "Invalid payload manifest")?;
    let backend = if entries.contains_key(AP_UNIT) {
        "create_ap"
    } else {
        "networkmanager"
    };
    let unit = if backend == "create_ap" {
        AP_UNIT
    } else {
        NM_UNIT
    };
    let mut expected = vec![SERVICE, LICENSE, POLICY, POLKIT, ACTIVATION, unit];
    if backend == "create_ap" {
        expected.push(HELPER);
    }
    expected.sort_unstable();
    if entries.keys().map(String::as_str).collect::<Vec<_>>() != expected {
        return Err("Payload must contain exactly one complete native backend");
    }
    let mut files = slots();
    let mut blobs = BTreeMap::new();
    for (path, expected) in entries {
        let mode = if path == SERVICE || path == HELPER {
            0o755
        } else {
            0o644
        };
        if expected.mode != format!("0o{mode:o}") || !hex(&expected.sha256, 64) {
            return Err("Invalid payload hash or permissions");
        }
        let data = root.read(&path, LIMIT)?.ok_or("Payload file is missing")?;
        if data.mode != mode || digest(&data.bytes) != expected.sha256 {
            return Err("Payload bytes or mode differ from manifest");
        }
        let template: Option<&[u8]> = match path.as_str() {
            NM_UNIT => Some(include_bytes!(
                "../../../packaging/relayforge-native.service"
            )),
            AP_UNIT => Some(include_bytes!(
                "../../../packaging/relayforge-create-ap.service"
            )),
            ACTIVATION => Some(if backend == "create_ap" {
                include_bytes!("../../../packaging/create-ap-dbus.service").as_slice()
            } else {
                include_bytes!("../../../packaging/io.github.erhanzeyrek.WifiHotspot.service")
                    .as_slice()
            }),
            POLICY => Some(include_bytes!(
                "../../../vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.conf"
            )),
            POLKIT => Some(include_bytes!(
                "../../../vendor/wifi-relay/data/io.github.erhanzeyrek.WifiHotspot.policy"
            )),
            LICENSE => Some(include_bytes!("../../../LICENSE")),
            _ => None,
        };
        if template.is_some_and(|template| template != data.bytes) {
            return Err("Payload integration files differ from reviewed templates");
        }
        if (path == SERVICE || path == HELPER)
            && (data.bytes.len() < 64 || !data.bytes.starts_with(b"\x7fELF\x02\x01\x01"))
        {
            return Err("Payload executable must be a regular native ELF64 binary");
        }
        let mut entry = Entry::of(&data);
        entry.gid = unsafe { libc::getegid() };
        blobs.insert(entry.sha256.clone(), data.bytes);
        files.insert(path, Some(entry));
    }
    if backend == "create_ap" && files[SERVICE] != files[HELPER] {
        return Err("Native helper and service must be identical");
    }
    Ok(Payload {
        backend: backend.into(),
        files,
        blobs,
    })
}
fn configuration(root: &Root, backend: &str) -> Result<()> {
    let data = root
        .read("etc/wifi-hotspot.conf", 65536)?
        .ok_or("Initialize private offline configuration before installation")?;
    if data.mode != 0o600 {
        return Err("Offline configuration must have mode 0600");
    }
    let text =
        std::str::from_utf8(&data.bytes).map_err(|_| "Invalid offline configuration encoding")?;
    let records = relayforge_core::config::parse_records(text)?;
    if !records.contains_key("PASSPHRASE") {
        return Err("Offline configuration requires explicit credentials");
    }
    let config = relayforge_core::config::validate(&records)?;
    if config["BACKEND"] != backend {
        return Err("Offline configuration backend differs from deployment");
    }
    Ok(())
}
fn quiescent(root: &Root, reserve: bool, fixture: bool) -> Result<Vec<File>> {
    let mut locks = vec![];
    let check = || -> Result<()> {
        if root.entries("run/relayforge")?.iter().any(|v| v != ".lock")
            || !root.entries("run/wifi-relay")?.is_empty()
        {
            return Err(
            "Pending networking evidence blocks deployment; use matching old service for cleanup",
        );
        }
        Ok(())
    };
    check()?;
    fault(fixture, root, "runtime-scan")?;
    if reserve {
        root.private_dir("run/relayforge")?;
    }
    if reserve || root.read("run/relayforge/.lock", 64)?.is_some() {
        locks.push(root.lock("run/relayforge/.lock")?);
    }
    // Evidence may have appeared between the first scan and lock acquisition.
    check()?;
    Ok(locks)
}
fn store_blobs(root: &Root, blobs: &BTreeMap<String, Vec<u8>>) -> Result<()> {
    for (hash, bytes) in blobs {
        let path = format!("{STATE}/blobs/{hash}");
        match root.read(&path, LIMIT)? {
            Some(data) if data.mode == 0o600 && data.bytes == *bytes => {}
            Some(_) => return Err("Saved deployment blob was changed"),
            None => root.write(
                &path,
                &Data {
                    bytes: bytes.clone(),
                    mode: 0o600,
                    gid: unsafe { libc::getegid() },
                },
            )?,
        }
    }
    Ok(())
}
fn verify(actual: &Files, before: &Files, after: Option<&Files>) -> Result<()> {
    for path in PATHS {
        if actual[path] != before[path] && after.is_none_or(|after| actual[path] != after[path]) {
            return Err("Installed files changed externally; deployment retained for review");
        }
    }
    Ok(())
}
fn materialize(root: &Root, snapshot: &Snapshot) -> Result<BTreeMap<String, Data>> {
    let mut files = BTreeMap::new();
    for (path, entry) in &snapshot.files {
        if let Some(entry) = entry {
            let data = root
                .read(&format!("{STATE}/blobs/{}", entry.sha256), LIMIT)?
                .ok_or("Saved deployment blob is missing")?;
            if data.mode != 0o600
                || data.bytes.len() != entry.size
                || digest(&data.bytes) != entry.sha256
            {
                return Err("Saved deployment blob failed verification");
            }
            files.insert(
                path.clone(),
                Data {
                    bytes: data.bytes,
                    mode: entry.mode,
                    gid: entry.gid,
                },
            );
        }
    }
    Ok(files)
}
fn fault(fixture: bool, root: &Root, phase: &str) -> Result<()> {
    if fixture {
        if let Some(data) = root.read("deployment-fault.json", 1024)? {
            let value: serde_json::Value =
                serde_json::from_slice(&data.bytes).map_err(|_| "Invalid fixture fault")?;
            if value["after"] == phase {
                return Err("Simulated offline deployment interruption");
            }
            if value["pause"] == phase {
                root.write(
                    "deployment-paused",
                    &Data {
                        bytes: phase.as_bytes().to_vec(),
                        mode: 0o600,
                        gid: unsafe { libc::getegid() },
                    },
                )?;
                let start = std::time::Instant::now();
                while root.read("deployment-continue", 64)?.is_none() {
                    if start.elapsed() > std::time::Duration::from_secs(10) {
                        return Err("Timed out waiting for fixture continuation");
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            }
        }
    }
    Ok(())
}
fn apply(root: &Root, state: &mut State, fixture: bool) -> Result<()> {
    let pending = state.pending.as_ref().ok_or("No pending deployment")?;
    let abort = state.aborting;
    let destination = if abort { &state.current } else { pending };
    let files = materialize(root, destination)?;
    let (actual, _) = capture(root)?;
    verify(&actual, &state.current.files, Some(&pending.files))?;
    configuration(root, &state.backend)?;
    for (index, path) in PATHS.into_iter().enumerate() {
        let actual = root.read(path, LIMIT)?.as_ref().map(Entry::of);
        if actual != state.current.files[path] && actual != pending.files[path] {
            return Err("Deployment target changed during transaction");
        }
        if actual != destination.files[path] {
            if let Some(data) = files.get(path) {
                root.write(path, data)?;
            } else {
                root.remove(path)?;
            }
        }
        // A previous process may have published this name and died before its
        // directory sync. Matching bytes alone are not evidence of durability.
        root.sync_path(path)?;
        fault(fixture, root, &format!("file-{index}"))?;
    }
    fault(fixture, root, "files")?;
    configuration(root, &state.backend)?;
    let (actual, _) = capture(root)?;
    verify(&actual, &destination.files, None)?;
    if !abort {
        let next = state.pending.take().ok_or("Missing pending target")?;
        state.previous = Some(std::mem::replace(&mut state.current, next));
    }
    state.pending = None;
    state.aborting = false;
    save(root, state)?;
    fault(fixture, root, "commit")
}

pub(crate) fn run(args: &[String], fixture: bool) -> Result<()> {
    let uid = unsafe { libc::geteuid() };
    if fixture && uid == 0 || !fixture && uid != 0 {
        return Err("Production deployment requires root; fixtures require an ordinary user");
    }
    let (action,path,source)=match args {
        [action,flag,path] if flag=="--root"=>(action.as_str(),path.as_str(),None),
        [action,flag,path,payload_flag,source] if flag=="--root"&&payload_flag=="--payload"=>(action.as_str(),path.as_str(),Some(source.as_str())),
        _=>return Err("Usage: --deploy plan|install|status|rollback|recover --root OFFLINE_ROOT [--payload STAGED_PAYLOAD]"),
    };
    if !matches!(
        action,
        "plan" | "install" | "status" | "rollback" | "recover"
    ) || matches!(action, "plan" | "install") != source.is_some()
    {
        return Err("Invalid deployment action or payload option");
    }
    let root = Root::open(Path::new(path), uid)?;
    let payload = source
        .map(|path| payload(Path::new(path), uid))
        .transpose()?;
    let _runtime_locks = quiescent(&root, !matches!(action, "plan" | "status"), fixture)?;
    let mut state = load(&root)?;
    let (before, before_blobs) = capture(&root)?;
    if let Some(state) = &state {
        verify(
            &before,
            &state.current.files,
            state.pending.as_ref().map(|s| &s.files),
        )?;
    }
    if action == "status" {
        println!(
            "{}",
            json!({"installed":state.is_some(),"pending":state.as_ref().is_some_and(|s|s.pending.is_some()),"revision":state.as_ref().map(|s|&s.current.id),"rollback_available":state.as_ref().is_some_and(|s|s.previous.is_some())})
        );
        return Ok(());
    }
    if let Some(payload) = &payload {
        configuration(&root, &payload.backend)?;
        if state
            .as_ref()
            .is_some_and(|state| state.backend != payload.backend || state.pending.is_some())
        {
            return Err("Backend switch or pending transaction requires explicit recovery");
        }
    }
    if action == "plan" {
        let payload = payload.as_ref().ok_or("Missing payload")?;
        let changed: Vec<_> = PATHS
            .into_iter()
            .filter(|path| before[*path] != payload.files[*path])
            .collect();
        println!(
            "{}",
            json!({"backend":payload.backend,"operation":if state.is_some(){"upgrade"}else{"install"},"changed":changed,"configuration":"preserved","activation":"offline-only; no services contacted"})
        );
        return Ok(());
    }
    root.private_dir(STATE)?;
    root.private_dir(&format!("{STATE}/blobs"))?;
    let _lock = root.lock(&format!("{STATE}/.lock"))?;
    fault(fixture, &root, "locked")?;
    // Re-read after locking; a competing deployment may have committed while we prepared.
    state = load(&root)?;
    let (actual, _) = capture(&root)?;
    if actual != before {
        return Err("Deployment changed while acquiring lock");
    }
    if let Some(state) = &state {
        verify(
            &actual,
            &state.current.files,
            state.pending.as_ref().map(|s| &s.files),
        )?;
    }
    if action == "install" {
        let payload = payload.ok_or("Missing payload")?;
        if state
            .as_ref()
            .is_some_and(|state| state.backend != payload.backend || state.pending.is_some())
        {
            return Err("Backend switch or pending transaction requires explicit recovery");
        }
        if state
            .as_ref()
            .is_some_and(|state| state.current.files == payload.files)
        {
            println!(
                "{}",
                json!({"success":true,"already_installed":true,"activated":false})
            );
            return Ok(());
        }
        store_blobs(&root, &before_blobs)?;
        store_blobs(&root, &payload.blobs)?;
        let mut current = state.unwrap_or(State {
            version: 1,
            root: root.identity()?,
            backend: payload.backend,
            current: Snapshot {
                id: random_secret()?,
                files: before,
            },
            previous: None,
            pending: None,
            aborting: false,
        });
        current.pending = Some(Snapshot {
            id: random_secret()?,
            files: payload.files,
        });
        save(&root, &current)?;
        fault(fixture, &root, "journal")?;
        apply(&root, &mut current, fixture)?;
        println!(
            "{}",
            json!({"success":true,"revision":current.current.id,"configuration":"preserved","activated":false})
        );
        return Ok(());
    }
    let mut state = state.ok_or("No managed deployment exists")?;
    if action == "recover" && state.pending.is_none() {
        println!(
            "{}",
            json!({"success":true,"pending":false,"activated":false})
        );
        return Ok(());
    }
    if action == "rollback" && state.pending.is_none() {
        let previous = state
            .previous
            .clone()
            .ok_or("No rollback snapshot exists")?;
        // Refuse an unusable rollback before replacing the stable receipt.
        materialize(&root, &previous)?;
        configuration(&root, &state.backend)?;
        state.pending = Some(previous);
        save(&root, &state)?;
        fault(fixture, &root, "journal")?;
        apply(&root, &mut state, fixture)?;
    } else {
        if action == "rollback" && !state.aborting {
            materialize(&root, &state.current)?;
            configuration(&root, &state.backend)?;
            state.aborting = true;
            save(&root, &state)?;
            fault(fixture, &root, "abort-journal")?;
        }
        apply(&root, &mut state, fixture)?;
    }
    println!(
        "{}",
        json!({"success":true,"revision":state.current.id,"activated":false})
    );
    Ok(())
}
