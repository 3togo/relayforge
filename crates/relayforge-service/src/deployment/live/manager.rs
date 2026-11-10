//! Bounded, owner-pinned systemd D-Bus calls. No subprocesses or shell commands.
use super::*;
use std::{future::Future, io::Read, os::unix::fs::MetadataExt};
use zbus::{
    zvariant::{OwnedObjectPath, OwnedValue},
    Proxy,
};
const SYSTEMD: &str = "org.freedesktop.systemd1";
const PATH: &str = "/org/freedesktop/systemd1";
const MANAGER: &str = "org.freedesktop.systemd1.Manager";
const UNIT: &str = "org.freedesktop.systemd1.Unit";
type Row = (
    String,
    String,
    String,
    String,
    String,
    String,
    OwnedObjectPath,
    u32,
    String,
    OwnedObjectPath,
);
async fn bounded<T, E>(future: impl Future<Output = std::result::Result<T, E>>) -> Result<T> {
    timeout(Duration::from_secs(8), future)
        .await
        .map_err(|_| "Service manager call timed out")?
        .map_err(|_| "Service manager or deployment gate rejected the operation")
}
pub(super) struct Manager {
    connection: Connection,
    pub owner: String,
    pub bus: String,
    fixture: bool,
}
impl Manager {
    pub async fn connect(fixture: bool) -> Result<Self> {
        let connection = if fixture {
            bounded(Connection::session()).await?
        } else {
            let builder =
                zbus::connection::Builder::address("unix:path=/run/dbus/system_bus_socket")
                    .map_err(|_| "Cannot select system bus")?;
            bounded(builder.build()).await?
        };
        let dbus = bounded(fdo::DBusProxy::new(&connection)).await?;
        let owner = bounded(dbus.get_name_owner(SYSTEMD.try_into().unwrap()))
            .await?
            .to_string();
        let bus = bounded(dbus.get_id()).await?.to_string();
        if !fixture
            && bounded(dbus.get_connection_unix_user(owner.as_str().try_into().unwrap())).await?
                != 0
        {
            return Err("System service manager must be owned by root");
        }
        Ok(Self {
            connection,
            owner,
            bus,
            fixture,
        })
    }
    async fn identity(&self) -> Result<()> {
        let dbus = bounded(fdo::DBusProxy::new(&self.connection)).await?;
        let owner = bounded(dbus.get_name_owner(SYSTEMD.try_into().unwrap())).await?;
        if owner.as_str() != self.owner {
            return Err("Service manager owner changed during handover");
        }
        Ok(())
    }
    async fn proxy(&self) -> Result<Proxy<'_>> {
        self.identity().await?;
        bounded(Proxy::new(
            &self.connection,
            self.owner.as_str(),
            PATH,
            MANAGER,
        ))
        .await
    }
    async fn owner(&self) -> Result<Option<String>> {
        let proxy = bounded(fdo::DBusProxy::new(&self.connection)).await?;
        match timeout(
            Duration::from_secs(8),
            proxy.get_name_owner(BUS_NAME.try_into().unwrap()),
        )
        .await
        {
            Ok(Ok(value)) => Ok(Some(value.to_string())),
            Ok(Err(fdo::Error::NameHasNoOwner(_))) => Ok(None),
            _ => Err("Cannot identify compatibility bus owner"),
        }
    }
    pub async fn no_owner(&self) -> Result<()> {
        if self.owner().await?.is_some() {
            return Err("Unexpected compatibility bus owner");
        }
        Ok(())
    }
    pub async fn stopped(&self) -> Result<bool> {
        if self.active().await?.is_none() {
            self.no_owner().await?;
            Ok(true)
        } else {
            Ok(false)
        }
    }
    async fn raw_rows(&self) -> Result<Vec<Row>> {
        let proxy = self.proxy().await?;
        let rows: Vec<Row> = bounded(proxy.call(
            "ListUnitsByPatterns",
            &(Vec::<&str>::new(), UNITS.as_slice()),
        ))
        .await?;
        if rows.len() > UNITS.len() {
            return Err("Unexpected handover units");
        }
        let mut names = std::collections::BTreeSet::new();
        for row in &rows {
            if !UNITS.contains(&row.0.as_str())
                || !names.insert(&row.0)
                || !row.5.is_empty()
            {
                return Err("Service jobs, aliases or transitional states block handover");
            }
        }
        Ok(rows)
    }
    async fn rows(&self) -> Result<Vec<Row>> {
        let rows = self.raw_rows().await?;
        if rows.iter().any(|r| r.7 != 0 || !matches!(r.3.as_str(), "active" | "inactive" | "failed")) {
            return Err("Service jobs or transitional states block handover");
        }
        Ok(rows)
    }
    async fn active(&self) -> Result<Option<String>> {
        let rows = self.rows().await?;
        let active: Vec<_> = rows.iter().filter(|v| v.3 == "active").collect();
        if active.len() > 1 {
            return Err("Multiple sharing services are active");
        }
        Ok(active.first().map(|v| v.0.clone()))
    }
    async fn property(
        &self,
        path: &OwnedObjectPath,
        interface: &str,
        property: &str,
    ) -> Result<OwnedValue> {
        let proxy = bounded(Proxy::new(
            &self.connection,
            self.owner.as_str(),
            path.as_str(),
            "org.freedesktop.DBus.Properties",
        ))
        .await?;
        bounded(proxy.call("Get", &(interface, property))).await
    }
    async fn definition(&self, unit: &str) -> Result<OwnedObjectPath> {
        let proxy = self.proxy().await?;
        let path: OwnedObjectPath = bounded(proxy.call("LoadUnit", &(unit,))).await?;
        let fragment = String::try_from(self.property(&path, UNIT, "FragmentPath").await?)
            .map_err(|_| "Invalid unit fragment")?;
        let dropins = Vec::<String>::try_from(self.property(&path, UNIT, "DropInPaths").await?)
            .map_err(|_| "Invalid unit overrides")?;
        if fragment != format!("/usr/lib/systemd/system/{unit}") || !dropins.is_empty() {
            return Err("Live handover refuses overridden service definitions");
        }
        Ok(path)
    }
    pub async fn preflight(&self, before: &Snapshot, after: &Snapshot, uid: u32) -> Result<()> {
        if self.rows().await?.iter().any(|r| r.3 == "failed") {
            return Err("Resolve failed services before beginning a new handover");
        }
        let current = unit(&before.files)?;
        let target = unit(&after.files)?;
        let proxy = self.proxy().await?;
        for unit in UNITS {
            let state: std::result::Result<String, zbus::Error> = timeout(
                Duration::from_secs(8),
                proxy.call("GetUnitFileState", &(unit,)),
            )
            .await
            .map_err(|_| "Unit file query timed out")?;
            match state {
                Ok(value)
                    if matches!(
                        value.as_str(),
                        "enabled"
                            | "enabled-runtime"
                            | "disabled"
                            | "static"
                            | "indirect"
                            | "not-found"
                    ) =>
                {
                    // /etc unit files outrank /run masks. Check inactive and
                    // fresh-install conflicts too, before relying on masks.
                    if value != "not-found" {
                        self.definition(unit).await?;
                    }
                }
                Err(zbus::Error::MethodError(name, _, _))
                    if matches!(
                        name.as_str(),
                        "org.freedesktop.systemd1.NoSuchUnit"
                            | "org.freedesktop.DBus.Error.FileNotFound"
                    ) => {}
                _ => return Err("Masks, aliases or unsupported unit file states block handover"),
            }
        }
        let active = self.active().await?;
        if let Some(active) = active {
            if Some(active.as_str()) != current {
                return Err("Active service does not match installed native snapshot");
            }
            self.definition(&active).await?;
            let owner = self.verify_process(&active, before.files[SERVICE].as_ref().unwrap(), uid)
                .await?;
            self.idle(&owner).await?;
            if self.maintenance_state().await? {
                return Err("Service is already in maintenance without this handover");
            }
        } else {
            self.no_owner().await?;
        }
        if let Some(target) = target {
            if before.files[if target == UNITS[3] { AP_UNIT } else { NM_UNIT }].is_some() {
                self.definition(target).await?;
            }
        }
        Ok(())
    }
    async fn maintenance_state(&self) -> Result<bool> {
        let owner = self
            .owner()
            .await?
            .ok_or("No compatibility owner for maintenance")?;
        let proxy = bounded(Proxy::new(
            &self.connection,
            owner.as_str(),
            GATE_PATH,
            GATE,
        ))
        .await?;
        bounded(proxy.call("GetMaintenance", &())).await
    }
    pub async fn maintenance(&self, owner: &str, enabled: bool) -> Result<()> {
        if self.owner().await?.as_deref() != Some(owner) {
            return Err("Verified service owner changed before maintenance");
        }
        let proxy = bounded(Proxy::new(
            &self.connection,
            owner,
            GATE_PATH,
            GATE,
        ))
        .await?;
        let (): () = bounded(proxy.call("SetMaintenance", &(enabled,))).await?;
        if self.owner().await?.as_deref() != Some(owner) {
            return Err("Service owner changed during maintenance");
        }
        Ok(())
    }
    async fn idle(&self, owner: &str) -> Result<()> {
        if self.owner().await?.as_deref() != Some(owner) { return Err("Verified service owner changed before status"); }
        let proxy = bounded(Proxy::new(
            &self.connection,
            owner,
            OBJECT_PATH,
            BUS_NAME,
        ))
        .await?;
        let text: String = bounded(proxy.call("GetStatus", &())).await?;
        if text.len() > 65536 {
            return Err("Service status exceeds deployment limit");
        }
        let value: serde_json::Value =
            serde_json::from_str(&text).map_err(|_| "Invalid deployment service status")?;
        if value["active"] != false
            || value["desired_active"] != false
            || !matches!(value["state"].as_str(), Some("idle" | ""))
        {
            return Err("Sharing must be idle before live handover");
        }
        if self.owner().await?.as_deref() != Some(owner) { return Err("Service owner changed during status"); }
        Ok(())
    }
    pub async fn quiesce(&self, state: &State, uid: u32) -> Result<()> {
        match self.active().await? {
            Some(unit)
                if matches!(
                    unit.as_str(),
                    "relayforge-native.service" | "relayforge-create-ap.service"
                ) =>
            {
                let mut recognized = None;
                for snapshot in [&state.current, state.pending.as_ref().ok_or("Missing live target")?] {
                    if super::unit(&snapshot.files)? == Some(unit.as_str()) {
                        if let Ok(owner) = self.verify_process(&unit, snapshot.files[SERVICE].as_ref().unwrap(), uid).await {
                            recognized = Some(owner); break;
                        }
                    }
                }
                let owner = recognized.ok_or("Unrecognized native service generation during recovery")?;
                self.maintenance(&owner, true).await?;
                self.idle(&owner).await
            }
            Some(_) => Err("Legacy services require explicit offline adoption"),
            None => self.no_owner().await,
        }
    }
    pub async fn mask(&self, units: &[&str]) -> Result<()> {
        let changes: Vec<(String, String, String)> = bounded(
            self.proxy()
                .await?
                .call("MaskUnitFiles", &(units, true, false)),
        )
        .await?;
        let _ = changes;
        Ok(())
    }
    pub async fn unmask(&self, units: &[&str]) -> Result<()> {
        let _: Vec<(String, String, String)> =
            bounded(self.proxy().await?.call("UnmaskUnitFiles", &(units, true))).await?;
        Ok(())
    }
    pub async fn reload(&self) -> Result<()> {
        bounded(self.proxy().await?.call::<_, _, ()>("Reload", &())).await
    }
    pub async fn reload_bus(&self) -> Result<()> {
        let proxy = bounded(fdo::DBusProxy::new(&self.connection)).await?;
        bounded(proxy.reload_config()).await
    }
    /// Settle jobs left by an interrupted handover before verifying and stopping
    /// the recognized service. Never cancel an unrelated activation to recover.
    pub async fn drain(&self, state: &State, uid: u32, starting: bool) -> Result<()> {
        let pending = state.pending.as_ref().ok_or("Missing live target")?;
        let owned_units = [
            super::unit(&state.current.files)?,
            super::unit(&pending.files)?,
        ];
        timeout(
            Duration::from_secs(if self.fixture { 5 } else { 120 }),
            async {
                loop {
                    let rows = self.raw_rows().await?;
                    let mut settling = false;
                    for row in &rows {
                        if row.7 == 0 && matches!(row.3.as_str(), "active" | "inactive" | "failed")
                        {
                            continue;
                        }
                        let expected_state = matches!(
                            row.3.as_str(),
                            "active" | "inactive" | "failed" | "deactivating"
                        ) || starting && row.3 == "activating";
                        let expected_job =
                            matches!(row.8.as_str(), "" | "stop") || starting && row.8 == "start";
                        if !owned_units.contains(&Some(row.0.as_str()))
                            || !expected_state
                            || !expected_job
                        {
                            return Err("Unrecognized service job during live recovery");
                        }
                        settling = true;
                    }
                    if !settling {
                        self.quiesce(state, uid).await?;
                        return self.stop_all().await;
                    }
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            },
        )
        .await
        .map_err(|_| "Service jobs did not settle; handover retained")?
    }
    async fn stop_all(&self) -> Result<()> {
        for row in self.rows().await? {
            if row.3 != "inactive" {
                let _: OwnedObjectPath =
                    bounded(self.proxy().await?.call("StopUnit", &(&row.0, "fail"))).await?;
            }
        }
        let deadline =
            tokio::time::Instant::now() + Duration::from_secs(if self.fixture { 5 } else { 120 });
        loop {
            // Job/transitional states are expected while StopUnit is executing.
            let rows = self.raw_rows().await?;
            if rows.iter().all(|r| r.3 == "inactive" && r.7 == 0) {
                self.no_owner().await?;
                return Ok(());
            }
            if tokio::time::Instant::now() >= deadline {
                return Err("Service shutdown was not confirmed; handover retained");
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
    pub async fn start(&self, unit: &str) -> Result<()> {
        self.definition(unit).await?;
        let _: OwnedObjectPath =
            bounded(self.proxy().await?.call("StartUnit", &(unit, "fail"))).await?;
        Ok(())
    }
    pub async fn verify_process(&self, unit: &str, entry: &Entry, uid: u32) -> Result<String> {
        if self.active().await?.as_deref() != Some(unit) {
            return Err("Expected native service is not active");
        }
        let owner = self
            .owner()
            .await?
            .ok_or("Expected native service has no bus name")?;
        let dbus = bounded(fdo::DBusProxy::new(&self.connection)).await?;
        let name: zbus::names::BusName<'_> = owner
            .as_str()
            .try_into()
            .map_err(|_| "Invalid unique owner")?;
        if bounded(dbus.get_connection_unix_user(name.clone())).await? != uid {
            return Err("Unexpected service owner UID");
        }
        let pid = bounded(dbus.get_connection_unix_process_id(name)).await?;
        let path: OwnedObjectPath = bounded(self.proxy().await?.call("GetUnit", &(unit,))).await?;
        let main = u32::try_from(
            self.property(&path, "org.freedesktop.systemd1.Service", "MainPID")
                .await?,
        )
        .map_err(|_| "Invalid service PID")?;
        let mapped: OwnedObjectPath =
            bounded(self.proxy().await?.call("GetUnitByPID", &(pid,))).await?;
        if pid == 0 || main != pid || mapped != path {
            return Err("Bus owner is not the managed service main process");
        }
        let file = File::open(format!("/proc/{pid}/exe"))
            .map_err(|_| "Cannot inspect running native executable")?;
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect executable metadata")?;
        if !meta.is_file() || meta.uid() != uid || meta.len() != entry.size as u64 {
            return Err("Running executable differs from deployment");
        }
        let mut bytes = Vec::new();
        file.take(LIMIT as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot verify running executable")?;
        if digest(&bytes) != entry.sha256 || self.owner().await?.as_deref() != Some(owner.as_str())
        {
            return Err("Running executable or bus owner changed during verification");
        }
        Ok(owner)
    }
    pub async fn ready(&self, unit: &str, entry: &Entry, uid: u32) -> Result<()> {
        let deadline =
            tokio::time::Instant::now() + Duration::from_secs(if self.fixture { 5 } else { 30 });
        loop {
            if let Ok(owner) = self.verify_process(unit, entry, uid).await {
                if self.maintenance_state().await == Ok(true) && self.idle(&owner).await.is_ok() {
                    return Ok(());
                }
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(
                    "Replacement native service failed readiness; use live recovery or rollback",
                );
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
}
