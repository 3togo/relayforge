//! Durable proof that networking was verified clean before deleting any records.
use super::*;
type Proof = (u64, u64, String);
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Completion {
    version: u32,
    snapshot: Snapshot,
    identity: (u64, u64),
    files: BTreeMap<String, Proof>,
    control: Option<(u64, u64)>,
    markers: BTreeMap<String, Option<Proof>>,
}
fn name(path: &Path) -> Result<String> {
    let name = path
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or("Invalid completed instance name")?;
    if !instance(name) {
        return Err("Invalid completed instance directory");
    }
    Ok(format!(".cleanup-{name}.json"))
}
fn marker_root(p: &Platform) -> &Path {
    p.fixture.as_deref().unwrap_or(Path::new("/run/relayforge"))
}
impl Completion {
    fn validate(&self, p: &Platform, path: &Path) -> Result<()> {
        self.snapshot.validate(p, path)?;
        if path.parent() != Some(p.root.as_path())
            || self.version != 1
            || self.identity.1 == 0
            || self.files.len() > 64
            || !self.files.contains_key("recovery.json")
            || self.files.keys().any(|n| !known(n, p.fixture.is_some()))
        {
            return Err("Invalid cleanup completion proof");
        }
        if let Some(id) = &self.snapshot.directory {
            if self.identity != (id.device, id.inode) {
                return Err("Cleanup directory binding changed");
            }
        }
        for (_, _, hash) in self.files.values().chain(self.markers.values().flatten()) {
            if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err("Invalid cleanup file fingerprint");
            }
        }
        let marker = Path::new(&self.snapshot.fields["DAEMON_PIDFILE"])
            .file_name()
            .unwrap()
            .to_str()
            .unwrap();
        if self.markers.keys().cloned().collect::<Vec<_>>()
            != vec![marker.to_string(), format!("{marker}.ready")]
        {
            return Err("Invalid cleanup marker set");
        }
        Ok(())
    }
}
fn read(p: &Platform, root: &Directory, path: &Path) -> Result<Option<(Completion, Vec<u8>)>> {
    root.read(&name(path)?, true, LIMIT)?
        .map(|bytes| {
            let record: Completion = serde_json::from_slice(&bytes)
                .map_err(|_| "Malformed cleanup completion record")?;
            record.validate(p, path)?;
            Ok((record, bytes))
        })
        .transpose()
}
fn unlink(dir: &Directory, name: &str, flags: i32) -> Result<()> {
    current_directory(dir)?;
    let name = filename(name)?;
    if unsafe { libc::unlinkat(dir.fd.as_raw_fd(), name.as_ptr(), flags) } != 0 {
        return Err("Cannot retire verified instance file");
    }
    dir.fd
        .sync_all()
        .map_err(|_| "Cannot sync instance retirement")
}
fn interruption(p: &Platform, stage: &str) -> Result<()> {
    if let Some(root) = &p.fixture {
        let dir = Directory::open(root)?;
        if let Some(bytes) = dir.read("cleanup-control.json", true, LIMIT)? {
            let control: serde_json::Value =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid cleanup fixture control")?;
            if control["after"].as_str() == Some(stage) {
                return Err("Simulated interruption of instance retirement");
            }
        }
    }
    Ok(())
}
fn finish(p: &Platform, root: &Directory, path: &Path, record: &Completion) -> Result<()> {
    let markers = Directory::open(marker_root(p))?;
    // Check the whole remaining tree before deleting anything. Missing files are
    // permitted only because this durable record proves resource cleanup finished.
    let dir = match fs::symlink_metadata(path) {
        Ok(meta) => {
            if !meta.is_dir() || (meta.dev(), meta.ino()) != record.identity {
                return Err("Completed instance directory was replaced");
            }
            let dir = p.open(path, false)?;
            let (files, control) = inventory(&dir, p.fixture.is_some())?;
            if files
                .iter()
                .any(|(n, proof)| record.files.get(n) != Some(proof))
                || control.is_some() && control != record.control
            {
                return Err("Completed instance files changed; retaining proof");
            }
            Some((dir, files, control))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(_) => return Err("Cannot inspect completed instance"),
    };
    let expected = format!("{}\n", record.snapshot.owner.pid).into_bytes();
    for (name, proof) in &record.markers {
        if let Some(bytes) = markers.read(name, true, 32)? {
            if bytes != expected || proof.as_ref() != Some(&file_proof(&markers, name)?) {
                return Err("Completed instance marker changed");
            }
        }
    }
    interruption(p, "proof")?;
    for (name, proof) in &record.markers {
        if markers.read(name, true, 32)?.is_some() {
            if proof.as_ref() != Some(&file_proof(&markers, name)?) {
                return Err("Instance marker replaced before deletion");
            }
            unlink(&markers, name, 0)?;
        }
    }
    interruption(p, "markers")?;
    if let Some((dir, files, control)) = dir {
        if let Some(identity) = control {
            let meta = fs::symlink_metadata(path.join("hostapd_ctrl"))
                .map_err(|_| "Control directory disappeared")?;
            if !meta.is_dir() || (meta.dev(), meta.ino()) != identity {
                return Err("Control directory identity changed");
            }
            unlink(&dir, "hostapd_ctrl", libc::AT_REMOVEDIR)?;
        }
        let mut files: Vec<_> = files.into_iter().collect();
        files.sort_by_key(|(name, _)| name == "recovery.json");
        for (name, proof) in files {
            if file_proof(&dir, &name)? != proof {
                return Err("Instance file replaced before deletion");
            }
            unlink(&dir, &name, 0)?;
            interruption(
                p,
                if name == "recovery.json" {
                    "snapshot"
                } else {
                    "file"
                },
            )?;
        }
        current_directory(&dir)?;
        fs::remove_dir(path).map_err(|_| "Cannot retire completed instance directory")?;
        root.fd
            .sync_all()
            .map_err(|_| "Cannot sync retired instance directory")?;
    }
    interruption(p, "directory")?;
    unlink(root, &name(path)?, 0)
}
pub(super) async fn create(p: &Platform, dir: &Directory) -> Result<()> {
    let root = Directory::open(&p.root)?;
    let _lock = root.transaction_lock(c".cleanup.lock").await?;
    if read(p, &root, &dir.path)?.is_some() {
        return Err("Finish pending instance retirement first");
    }
    let (snapshot, _) = snapshot(p, dir)?;
    let (files, control) = inventory(dir, p.fixture.is_some())?;
    let markers = Directory::open(marker_root(p))?;
    let marker = Path::new(&snapshot.fields["DAEMON_PIDFILE"])
        .file_name()
        .unwrap()
        .to_str()
        .unwrap();
    let expected = format!("{}\n", snapshot.owner.pid).into_bytes();
    let mut marker_files = BTreeMap::new();
    for name in [marker.to_string(), format!("{marker}.ready")] {
        let proof = match markers.read(&name, true, 32)? {
            Some(bytes) => {
                if bytes != expected {
                    return Err("Instance marker changed; retaining evidence");
                }
                Some(file_proof(&markers, &name)?)
            }
            None => None,
        };
        marker_files.insert(name, proof);
    }
    let meta = dir
        .fd
        .metadata()
        .map_err(|_| "Cannot bind completed instance")?;
    let record = Completion {
        version: 1,
        snapshot,
        identity: (meta.dev(), meta.ino()),
        files,
        control,
        markers: marker_files,
    };
    record.validate(p, &dir.path)?;
    root.write(&name(&dir.path)?, &record, None)?;
    finish(p, &root, &dir.path, &record)
}
pub(super) async fn recover(p: &Platform, path: &Path, require_dead: bool) -> Result<bool> {
    if !p.root.exists() {
        return Ok(false);
    }
    directory(&p.root, true)?;
    let root = Directory::open(&p.root)?;
    if read(p, &root, path)?.is_none() {
        return Ok(false);
    }
    let _lock = root.transaction_lock(c".cleanup.lock").await?;
    let Some((record, _)) = read(p, &root, path)? else {
        return Ok(false);
    };
    if require_dead && record.snapshot.owner.current()?.is_some() {
        return Err("Refusing retirement while its owner is alive");
    }
    finish(p, &root, path, &record)?;
    Ok(true)
}
pub(in crate::ap_runtime) async fn recover_marker(p: &Platform, marker: &Path) -> Result<()> {
    if !p.root.exists() {
        return Ok(());
    }
    directory(&p.root, true)?;
    let root = Directory::open(&p.root)?;
    let mut paths = Vec::new();
    for entry in fs::read_dir(&p.root).map_err(|_| "Cannot inspect pending retirements")? {
        let name = entry
            .map_err(|_| "Cannot inspect pending retirement")?
            .file_name()
            .into_string()
            .map_err(|_| "Invalid retirement filename")?;
        if let Some(instance) = name
            .strip_prefix(".cleanup-")
            .and_then(|v| v.strip_suffix(".json"))
        {
            let path = p.root.join(instance);
            if let Some((record, _)) = read(p, &root, &path)? {
                if Path::new(&record.snapshot.fields["DAEMON_PIDFILE"]) == marker {
                    paths.push(path);
                }
            }
        }
        if paths.len() > 64 {
            return Err("Too many matching pending retirements");
        }
    }
    for path in paths {
        recover(p, &path, true).await?;
    }
    Ok(())
}
