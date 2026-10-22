//! Explicit live handover. Never selected by offline deployment or environment.
use super::*;
use relayforge_core::{BUS_NAME, OBJECT_PATH};
use std::sync::atomic::Ordering;
use tokio::time::{timeout, Duration};
use zbus::{fdo, message::Header, Connection};
mod manager;
use manager::Manager;

pub(super) const RECORD: &str = "var/lib/relayforge-deploy/live.json";
pub(crate) const GATE_PATH: &str = "/io/github/relayforge/Deployment";
pub(crate) const PROTOCOL: &str = "relayforge:live-maintenance:v1";
pub(super) const GATE: &str = "io.github.relayforge.Deployment";
pub(super) const UNITS: [&str; 4] = [
    "wifi-hotspot-daemon.service",
    "relayforge.service",
    "relayforge-native.service",
    "relayforge-create-ap.service",
];
pub(crate) struct Gate {
    pub uid: u32,
}
fn supported(bytes: &[u8]) -> Result<()> {
    if !bytes
        .windows(PROTOCOL.len())
        .any(|v| v == PROTOCOL.as_bytes())
    {
        return Err("Target native binary lacks the live maintenance protocol; deploy it offline");
    }
    Ok(())
}
#[zbus::interface(name = "io.github.relayforge.Deployment")]
impl Gate {
    async fn get_maintenance(
        &self,
        #[zbus(connection)] connection: &Connection,
    ) -> fdo::Result<bool> {
        let interface = connection
            .object_server()
            .interface::<_, crate::Service>(OBJECT_PATH)
            .await?;
        let enabled = interface.get().await.maintenance.load(Ordering::SeqCst);
        Ok(enabled)
    }
    async fn set_maintenance(
        &self,
        enabled: bool,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<()> {
        let sender = header
            .sender()
            .ok_or_else(|| fdo::Error::AccessDenied("Missing sender".into()))?;
        let uid = fdo::DBusProxy::new(connection)
            .await?
            .get_connection_unix_user(sender.clone().into())
            .await?;
        if uid != self.uid {
            return Err(fdo::Error::AccessDenied(
                "Deployment requires the service administrator".into(),
            ));
        }
        let interface = connection
            .object_server()
            .interface::<_, crate::Service>(OBJECT_PATH)
            .await?;
        let service = interface.get().await;
        let _operation = service
            .operations
            .try_lock()
            .map_err(|_| fdo::Error::Failed("Service operation is in progress".into()))?;
        let _configuration = service
            .config_users
            .try_write()
            .map_err(|_| fdo::Error::Failed("Service configuration is in progress".into()))?;
        if enabled {
            let mut power = service.power.lock().unwrap();
            let core = service.core.lock().unwrap();
            if core.status.active
                || core.status.desired_active
                || !matches!(core.status.state.as_str(), "" | "idle")
                || power.blocked()
            {
                return Err(fdo::Error::Failed(
                    "Stop sharing and settle sleep recovery before deployment".into(),
                ));
            }
            power.cancel();
        }
        service.maintenance.store(enabled, Ordering::SeqCst);
        Ok(())
    }
}

#[derive(Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Prepared,
    Files,
    Starting,
    Ready,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Handover {
    version: u32,
    bus: String,
    manager: String,
    original: Option<State>,
    transaction: State,
    phase: Phase,
}
fn unit(files: &Files) -> Result<Option<&'static str>> {
    if files.values().all(Option::is_none) {
        return Ok(None);
    }
    if files[SERVICE].is_none() {
        return Err("Live handover requires a complete native snapshot");
    }
    let (selected, backend) = match (files[AP_UNIT].is_some(), files[NM_UNIT].is_some()) {
        (true, false) => (UNITS[3], "create_ap"),
        (false, true) => (UNITS[2], "networkmanager"),
        _ => return Err("Live handover refuses unmanaged or mixed backend snapshots"),
    };
    for path in PATHS {
        let expected =
            path != if backend == "create_ap" {
                NM_UNIT
            } else {
                AP_UNIT
            } && (path != HELPER || backend == "create_ap");
        if files[path].is_some() != expected {
            return Err("Live snapshot is incomplete");
        }
        if let Some(entry) = &files[path] {
            if entry.mode
                != if matches!(path, SERVICE | HELPER) {
                    0o755
                } else {
                    0o644
                }
                || integration(path, backend)
                    .is_some_and(|bytes| entry.sha256 != digest(bytes) || entry.size != bytes.len())
            {
                return Err("Live snapshot differs from reviewed native integration");
            }
        }
    }
    if backend == "create_ap" && files[SERVICE] != files[HELPER] {
        return Err("Live helper and service snapshots differ");
    }
    Ok(Some(selected))
}
fn destination(state: &State) -> Result<&Snapshot> {
    if state.aborting {
        Ok(&state.current)
    } else {
        state
            .pending
            .as_ref()
            .ok_or("Missing live deployment target")
    }
}
fn finished(state: &State) -> State {
    let mut next = state.clone();
    if !next.aborting {
        next.previous = Some(std::mem::replace(
            &mut next.current,
            next.pending.take().unwrap(),
        ));
    }
    next.pending = None;
    next.aborting = false;
    next
}
fn save_live(root: &Root, record: &Handover) -> Result<()> {
    record.transaction.validate(root)?;
    root.write(
        RECORD,
        &Data {
            bytes: serde_json::to_vec(record).map_err(|_| "Cannot encode live receipt")?,
            mode: 0o600,
            gid: unsafe { libc::getegid() },
        },
    )
}
fn load_live(root: &Root) -> Result<Option<Handover>> {
    let Some(data) = root.read(RECORD, 65536)? else {
        return Ok(None);
    };
    if data.mode != 0o600 {
        return Err("Live receipt must be private");
    }
    let record: Handover =
        serde_json::from_slice(&data.bytes).map_err(|_| "Invalid live receipt")?;
    record.transaction.validate(root)?;
    if let Some(original) = &record.original {
        original.validate(root)?;
        if original.pending.is_some() || original.backend != record.transaction.backend {
            return Err("Invalid original live deployment receipt");
        }
    }
    if record.version != 1
        || !hex(&record.bus, 32)
        || !record.manager.starts_with(':')
        || record.transaction.pending.is_none()
    {
        return Err("Invalid live handover identity");
    }
    let expected = if record.transaction.backend == "create_ap" {
        UNITS[3]
    } else {
        UNITS[2]
    };
    for snapshot in [
        &record.transaction.current,
        record.transaction.pending.as_ref().unwrap(),
    ] {
        if unit(&snapshot.files)?.is_some_and(|unit| unit != expected) {
            return Err("Live snapshot backend differs from receipt");
        }
    }
    Ok(Some(record))
}
fn compatible_receipt(root: &Root, record: &Handover) -> Result<()> {
    let actual = load(root)?;
    if record.phase == Phase::Ready {
        return if actual == Some(finished(&record.transaction)) {
            Ok(())
        } else {
            Err("Completed handover receipt changed externally")
        };
    }
    let mut forward = record.transaction.clone();
    forward.aborting = false;
    let mut abort = record.transaction.clone();
    abort.aborting = true;
    if actual == record.original
        || actual.as_ref().is_some_and(|s| {
            s == &forward || s == &abort || s == &finished(&forward) || s == &finished(&abort)
        })
    {
        Ok(())
    } else {
        Err("Deployment receipt changed outside live handover")
    }
}
fn evidence(root: &Root) -> Result<()> {
    if root.entries("run/relayforge")?.iter().any(|v| v != ".lock")
        || !root.entries("run/wifi-relay")?.is_empty()
    {
        return Err("Networking recovery evidence blocks live handover");
    }
    Ok(())
}

async fn execute(root: Root, action: &str, source: Option<&str>, fixture: bool) -> Result<()> {
    // All payload and backup validation precedes service mutations.
    let candidate = source
        .map(|path| payload(Path::new(path), root.uid))
        .transpose()?;
    if let Some(candidate) = &candidate {
        supported(&candidate.blobs[&candidate.files[SERVICE].as_ref().unwrap().sha256])?;
    }
    let existing = load(&root)?;
    let mut live = load_live(&root)?;
    let interrupted = live.is_some();
    let initial_live = root.read(RECORD, 65536)?.map(|v| v.bytes);
    if action == "status" {
        println!(
            "{}",
            json!({"pending":live.is_some(),"phase":live.as_ref().map(|v|v.phase),"aborting":live.as_ref().is_some_and(|v|v.transaction.aborting)})
        );
        return Ok(());
    }
    if live.is_some() && !matches!(action, "recover" | "rollback") {
        return Err("Pending live handover requires recovery or rollback");
    }
    let manager = Manager::connect(fixture).await?;
    if let Some(record) = &live {
        if record.bus != manager.bus || record.manager != manager.owner {
            return Err("Bus or service manager changed; retain handover for administrator review");
        }
        compatible_receipt(&root, record)?;
    }
    if action == "recover" && live.is_none() {
        println!("{}", json!({"success":true,"pending":false}));
        return Ok(());
    }
    let (before, blobs) = capture(&root)?;
    let mut prepared = if live.is_none() {
        evidence(&root)?;
        if let Some(state) = &existing {
            if state.pending.is_some() {
                return Err("Finish offline recovery before live handover");
            }
            verify(&before, &state.current.files, None)?;
        } else if before.values().any(Option::is_some) {
            return Err(
                "Live installation refuses an unmanaged predecessor; adopt it offline first",
            );
        }
        let mut state = if let Some(state) = &existing {
            state.clone()
        } else {
            State {
                version: 1,
                root: root.identity()?,
                backend: candidate
                    .as_ref()
                    .ok_or("No live rollback snapshot")?
                    .backend
                    .clone(),
                current: Snapshot {
                    id: random_secret()?,
                    files: before.clone(),
                },
                previous: None,
                pending: None,
                aborting: false,
            }
        };
        state.pending = Some(if let Some(candidate) = &candidate {
            if candidate.backend != state.backend {
                return Err("Live backend switching is disabled");
            }
            Snapshot {
                id: random_secret()?,
                files: candidate.files.clone(),
            }
        } else {
            state.previous.clone().ok_or("No live rollback snapshot")?
        });
        configuration(&root, &state.backend)?;
        unit(&state.current.files)?;
        unit(&state.pending.as_ref().unwrap().files)?;
        let expected_unit = if state.backend == "create_ap" {
            UNITS[3]
        } else {
            UNITS[2]
        };
        for snapshot in [&state.current, state.pending.as_ref().unwrap()] {
            if unit(&snapshot.files)?.is_some_and(|unit| unit != expected_unit) {
                return Err("Live snapshot backend differs from configuration");
            }
        }
        if candidate.is_none() {
            let target = materialize(&root, state.pending.as_ref().unwrap())?;
            if let Some(service) = target.get(SERVICE) {
                supported(&service.bytes)?;
            }
        }
        manager
            .preflight(&state.current, state.pending.as_ref().unwrap(), root.uid)
            .await?;
        Some(state)
    } else {
        None
    };
    if action == "plan" {
        println!(
            "{}",
            json!({"backend":prepared.as_ref().unwrap().backend,"handover":"idle native service only","sharing":"never restarted","configuration":"preserved"})
        );
        return Ok(());
    }
    root.private_dir(STATE)?;
    root.private_dir(&format!("{STATE}/blobs"))?;
    let _lock = root.lock(&format!("{STATE}/.lock"))?;
    // Preparation did not hold the lock. Reject every intervening receipt/file edit.
    if load(&root)? != existing
        || capture(&root)?.0 != before
        || root.read(RECORD, 65536)?.map(|v| v.bytes) != initial_live
    {
        return Err("Deployment changed while preparing live handover");
    }
    if let Some(state) = prepared.take() {
        if state.current.files == state.pending.as_ref().unwrap().files {
            println!("{}", json!({"success":true,"already_installed":true}));
            return Ok(());
        }
        manager
            .preflight(&state.current, state.pending.as_ref().unwrap(), root.uid)
            .await?;
        store_blobs(&root, &blobs)?;
        if let Some(candidate) = &candidate {
            store_blobs(&root, &candidate.blobs)?;
        }
        live = Some(Handover {
            version: 1,
            bus: manager.bus.clone(),
            manager: manager.owner.clone(),
            original: existing,
            transaction: state,
            phase: Phase::Prepared,
        });
        save_live(&root, live.as_ref().unwrap())?;
        fault(fixture, &root, "live-journal")?;
    }
    let mut record = live.ok_or("Missing live handover")?;
    compatible_receipt(&root, &record)?;
    if action == "rollback" && interrupted {
        if record.phase == Phase::Ready {
            return Err("Finish ready handover recovery before requesting another rollback");
        }
        materialize(&root, &record.transaction.current)?;
        configuration(&root, &record.transaction.backend)?;
        record.transaction.aborting = true;
        record.phase = Phase::Prepared;
        save_live(&root, &record)?;
        fault(fixture, &root, "live-abort-journal")?;
    }
    // The caller records whether this invocation is aborting an existing handover.
    continue_handover(&root, &manager, &mut record, fixture).await
}

async fn continue_handover(
    root: &Root,
    manager: &Manager,
    record: &mut Handover,
    fixture: bool,
) -> Result<()> {
    let target = destination(&record.transaction)?.clone();
    let target_unit = unit(&target.files)?;
    if record.phase != Phase::Ready {
        let files = materialize(root, &target)?;
        if let Some(service) = files.get(SERVICE) {
            supported(&service.bytes)?;
        }
        configuration(root, &record.transaction.backend)?;
        verify(
            &capture(root)?.0,
            &record.transaction.current.files,
            Some(&record.transaction.pending.as_ref().unwrap().files),
        )?;
        // A restarted/new service is also required to enter maintenance before stop.
        manager.quiesce(&record.transaction, root.uid).await?;
        manager.mask(&UNITS).await?;
        manager.reload().await?;
        fault(fixture, root, "live-masked")?;
        manager.stop_all().await?;
        fault(fixture, root, "live-stopped")?;
        let locks = quiescent(root, true, fixture)?;
        record.phase = Phase::Files;
        save_live(root, record)?;
        let mut transaction = record.transaction.clone();
        save(root, &transaction)?;
        apply(root, &mut transaction, fixture)?;
        fault(fixture, root, "live-files")?;
        manager.reload().await?;
        manager.reload_bus().await?;
        record.phase = Phase::Starting;
        save_live(root, record)?;
        drop(locks);
        fault(fixture, root, "live-starting")?;
        if let Some(unit) = target_unit {
            manager.unmask(&[unit]).await?;
            manager.reload().await?;
            manager.start(unit).await?;
            manager
                .ready(unit, target.files[SERVICE].as_ref().unwrap(), root.uid)
                .await?;
        } else {
            manager.no_owner().await?;
        }
        fault(fixture, root, "live-started")?;
        manager.unmask(&UNITS).await?;
        manager.reload().await?;
        record.phase = Phase::Ready;
        save_live(root, record)?;
        fault(fixture, root, "live-ready")?;
    }
    verify(&capture(root)?.0, &target.files, None)?;
    if let Some(unit) = target_unit {
        if manager.stopped().await? {
            evidence(root)?;
            let locks = quiescent(root, true, fixture)?;
            drop(locks);
            manager.start(unit).await?;
            manager
                .ready(unit, target.files[SERVICE].as_ref().unwrap(), root.uid)
                .await?;
        }
        manager
            .verify_process(unit, target.files[SERVICE].as_ref().unwrap(), root.uid)
            .await?;
        manager.maintenance(false).await?;
    } else {
        manager.no_owner().await?;
    }
    root.remove(RECORD)?;
    println!(
        "{}",
        json!({"success":true,"service":target_unit,"revision":target.id,"sharing_restarted":false,"configuration":"preserved"})
    );
    Ok(())
}

pub(crate) async fn run(args: &[String], fixture: bool) -> Result<()> {
    let uid = unsafe { libc::geteuid() };
    if fixture && uid == 0 || !fixture && uid != 0 {
        return Err("Live deployment requires root; fixtures require an ordinary user");
    }
    let (action, path, source) = match args {
        [action, flag, path] if flag == "--root" => (action.as_str(),path.as_str(),None),
        [action, flag, path, payload_flag, source] if flag == "--root" && payload_flag == "--payload" => (action.as_str(),path.as_str(),Some(source.as_str())),
        _ => return Err("Usage: --deploy-live plan|install|status|rollback|recover --root / [--payload STAGED_PAYLOAD]"),
    };
    if !matches!(
        action,
        "plan" | "install" | "status" | "rollback" | "recover"
    ) || matches!(action, "plan" | "install") != source.is_some()
    {
        return Err("Invalid live deployment action or payload");
    }
    let root = if fixture {
        Root::open(Path::new(path), uid)?
    } else {
        if path != "/" {
            return Err(
                "Live deployment requires explicit --root /; use --deploy for offline images",
            );
        }
        Root::live()?
    };
    timeout(
        Duration::from_secs(240),
        execute(root, action, source, fixture),
    )
    .await
    .map_err(|_| "Live handover timed out; retain receipts and recover")?
}
