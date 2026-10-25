mod ap_firewall;
mod ap_process;
mod ap_runtime;
mod backend;
mod config_store;
mod create_ap;
mod deployment;
mod inventory;
mod native_backend;
mod networkmanager;
mod nm_journal;
mod band_switch;
mod power;
mod radio;
mod radio_watch;
mod stale;
use backend::Worker;
use relayforge_core::{
    config::{self, parse_patch, Config},
    validate_reply, Lifecycle, Method, Status, BUS_NAME, CREDENTIALS_ACTION, MANAGE_ACTION,
    OBJECT_PATH,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use tokio::{
    sync::{mpsc, Semaphore},
    time::{timeout, Duration},
};
use zbus::{fdo, message::Header, zvariant::Value as BusValue, Connection, SignalContext};

/// Exit status for "never configured". The packaged units set
/// `RestartPreventExitStatus=` to this value so a missing file cannot loop.
const EXIT_MISSING_CONFIG: i32 = 17;

struct Service {
    core: Arc<Mutex<Lifecycle>>,
    clients: Arc<Mutex<Value>>,
    worker: Option<Arc<Worker>>,
    native: Option<Arc<native_backend::Backend>>,
    configuration: Arc<Mutex<config_store::Store>>,
    operations: tokio::sync::Mutex<()>,
    notifications: tokio::sync::Mutex<()>,
    config_users: tokio::sync::RwLock<()>,
    radio: radio::Source,
    cancellation: tokio::sync::watch::Sender<u64>,
    power: Mutex<power::State>,
    /// Last backend error already published as `UserActionRequired`; native
    /// monitoring mirrors the pinned poll, which only announces a *changed* error.
    reported_error: Mutex<Option<String>>,
    maintenance: std::sync::atomic::AtomicBool,
    fixture: bool,
    demo: bool,
    demo_deny: bool,
    use_polkit: bool,
    demo_generation: std::sync::atomic::AtomicU64,
    requests: Semaphore,
    stop_requests: Semaphore,
}

// Build the dispatcher before advertising the compatibility name. Otherwise a
// method arriving between name acquisition and registration can be lost by zbus 4.
struct Initializing;
#[zbus::interface(name = "io.github.relayforge.Initializing")]
impl Initializing {
    fn phase(&self) -> &str {
        "initializing"
    }
}

impl Service {
    async fn authorize(
        &self,
        connection: &Connection,
        header: &Header<'_>,
        action: &str,
    ) -> fdo::Result<()> {
        let sender = header
            .sender()
            .ok_or_else(|| fdo::Error::AccessDenied("Missing bus-assigned sender".into()))?;
        let check = async {
            if !self.use_polkit {
                if self.demo_deny {
                    return Ok(false);
                }
                let proxy = fdo::DBusProxy::new(connection).await?;
                let uid = proxy
                    .get_connection_unix_user(sender.clone().into())
                    .await?;
                // SAFETY: getuid has no preconditions and does not mutate process state.
                return Ok(uid == unsafe { libc::getuid() });
            }
            let proxy = zbus::Proxy::new(
                connection,
                "org.freedesktop.PolicyKit1",
                "/org/freedesktop/PolicyKit1/Authority",
                "org.freedesktop.PolicyKit1.Authority",
            )
            .await?;
            let subject = (
                "system-bus-name",
                HashMap::from([("name", BusValue::from(sender.as_str()))]),
            );
            let ((allowed, _, _details),): ((bool, bool, HashMap<String, String>),) = proxy
                .call(
                    "CheckAuthorization",
                    &(
                        subject,
                        action,
                        HashMap::<String, String>::new(),
                        1u32,
                        "",
                    ),
                )
                .await?;
            Ok::<bool, zbus::Error>(allowed)
        };
        match timeout(Duration::from_secs(65), check).await {
            Ok(Ok(true)) => Ok(()),
            _ => Err(fdo::Error::AccessDenied(
                "Authorization is required to manage sharing or read credentials.".into(),
            )),
        }
    }

    async fn emit_status(&self, connection: &Connection) {
        let _notification = self.notifications.lock().await;
        let data = serde_json::to_string(&self.core.lock().unwrap().status).unwrap();
        let _ = connection
            .emit_signal(
                None::<&str>,
                OBJECT_PATH,
                BUS_NAME,
                "StatusChanged",
                &(data,),
            )
            .await;
    }

    /// Broadcast a forced transitional state without storing it in the lifecycle.
    ///
    /// Native band switching never enters the lifecycle machine, so the pinned
    /// dispatcher's snapshot rule applies: the transition lives on the signal
    /// only and the next refresh reports the real state.
    async fn emit_transition(&self, connection: &Connection, state: &str) {
        let _notification = self.notifications.lock().await;
        let mut status = self.core.lock().unwrap().status.clone();
        status.state = state.into();
        let data = serde_json::to_string(&status).unwrap();
        let _ = connection
            .emit_signal(
                None::<&str>,
                OBJECT_PATH,
                BUS_NAME,
                "StatusChanged",
                &(data,),
            )
            .await;
    }

    async fn emit_clients(&self, connection: &Connection) {
        let _notification = self.notifications.lock().await;
        let data = self.clients.lock().unwrap().to_string();
        let _ = connection
            .emit_signal(
                None::<&str>,
                OBJECT_PATH,
                BUS_NAME,
                "ClientsChanged",
                &(data,),
            )
            .await;
    }

    /// Publish a native backend failure once per distinct message.
    ///
    /// The pinned daemon raises this from its periodic poll so the unchanged
    /// GNOME Shell/tray clients can notify the user; the error text is already
    /// redacted by the backend and never contains credentials.
    async fn announce_error(&self, connection: &Connection) {
        if self.native.is_none() {
            return;
        }
        let message = {
            let error = self.core.lock().unwrap().status.error.clone();
            let mut reported = self.reported_error.lock().unwrap();
            if error == *reported {
                return;
            }
            *reported = error.clone();
            error
        };
        let Some(message) = message else {
            return;
        };
        let data = json!({"type": "error", "message": message}).to_string();
        let _ = connection
            .emit_signal(
                None::<&str>,
                OBJECT_PATH,
                BUS_NAME,
                "UserActionRequired",
                &(data,),
            )
            .await;
    }

    async fn invoke(
        &self,
        method: Method,
        params: Value,
        connection: &Connection,
    ) -> fdo::Result<Value> {
        self.invoke_requested(method, params, connection, None)
            .await
    }

    async fn invoke_requested(
        &self,
        method: Method,
        params: Value,
        connection: &Connection,
        request: Option<tokio::sync::watch::Receiver<u64>>,
    ) -> fdo::Result<Value> {
        // Subscribe before the operation queue: Stop/sleep must cancel requests
        // already waiting there, not just the currently executing startup.
        let mut cancelled = request.unwrap_or_else(|| self.cancellation.subscribe());
        let cancelled_before_queue = cancelled.has_changed().unwrap_or(true);
        let request_revision = *cancelled.borrow();
        if method == Method::Stop {
            self.power.lock().unwrap().cancel();
            self.cancellation
                .send_modify(|revision| *revision = revision.wrapping_add(1));
        }
        let permits = if method == Method::Stop {
            &self.stop_requests
        } else {
            &self.requests
        };
        let _permit = permits
            .try_acquire()
            .map_err(|_| fdo::Error::LimitsExceeded("Too many pending requests".into()))?;
        // Serialize native configuration transactions with Start/band changes.
        // Stop keeps its own budget and bypasses this gate to cancel startup.
        let _operation = if method == Method::Stop {
            None
        } else if method == Method::Start {
            Some(tokio::select! {
                guard = self.operations.lock() => guard,
                _ = cancelled.changed() => return Ok(json!({"success":false,"error":"Startup cancelled while queued"})),
            })
        } else {
            Some(self.operations.lock().await)
        };
        // Start and Stop may overlap for cancellation, but neither may cross a
        // configuration commit. This also closes the idle-check/Stop race.
        let _save = if method == Method::SetConfig {
            Some(self.config_users.write().await)
        } else {
            None
        };
        let _backend = if matches!(
            method,
            Method::Start | Method::Stop | Method::SwitchBandAndReconnect | Method::PrepareFirewall
        ) {
            Some(self.config_users.read().await)
        } else {
            None
        };
        if method == Method::Start
            && (cancelled_before_queue || *cancelled.borrow() != request_revision)
        {
            return Ok(json!({"success":false,"error":"Startup cancelled while queued"}));
        }
        if self.maintenance.load(std::sync::atomic::Ordering::SeqCst)
            && matches!(
                method,
                Method::Start
                    | Method::SetConfig
                    | Method::SwitchBandAndReconnect
                    | Method::PrepareFirewall
            )
        {
            return Err(fdo::Error::Failed(
                "Service deployment is in progress; retry after handover".into(),
            ));
        }
        if self.native.is_some() {
            if method == Method::Start && self.power.lock().unwrap().blocked() {
                return Ok(
                    json!({"success":false,"error":"Sharing is suspended or sleep cleanup is pending"}),
                );
            }
            match method {
                Method::SwitchBandAndReconnect => {
                    // Reconnecting the upstream would tear down an owned AP's own
                    // uplink, so a switch is only offered while nothing is shared.
                    let busy = {
                        let core = self.core.lock().unwrap();
                        core.status.active
                            || core.status.desired_active
                            || matches!(
                                core.status.state.as_str(),
                                "connecting" | "stopping" | "waiting"
                            )
                    };
                    if busy {
                        return Ok(json!({"success":false,"error":"Stop sharing before switching the Wi-Fi band"}));
                    }
                    let target = params.as_str().unwrap_or_default().to_owned();
                    let config = self.load_config().await?;
                    // Same transitional broadcast as Start: the reconnect is
                    // visible to every client before the first bus round trip.
                    self.emit_transition(connection, "connecting").await;
                    let reply = band_switch::switch(self.fixture, &config, &target).await;
                    // A reconnect invalidates any armed resume intent and the radio
                    // snapshot every Start re-inspects.
                    self.power.lock().unwrap().cancel();
                    self.emit_status(connection).await;
                    return Ok(reply);
                }
                Method::PrepareFirewall => return Ok(json!(true)),
                _ => {}
            }
        }
        match method {
            Method::GetCapabilities => {
                let config = self.load_config().await?;
                let snapshot = self
                    .radio
                    .inspect(&config["WIFI_IFACE"])
                    .await
                    .map_err(|error| fdo::Error::Failed(error.into()))?;
                return Ok(json!(snapshot.capabilities));
            }
            Method::GetConfig => return self.load_config().await.map(|config| json!(config)),
            Method::SetConfig => {
                self.save_config(params).await?;
                self.emit_status(connection).await;
                return Ok(json!(true));
            }
            Method::GetInterfaces => {
                if self.demo {
                    return Ok(
                        json!({"wifi_interfaces":["demo0"],"all_interfaces":["demo0","demo1"]}),
                    );
                }
                return tokio::task::spawn_blocking(|| {
                    inventory::discover(Path::new("/sys/class/net"))
                })
                .await
                .map_err(|_| fdo::Error::Failed("Interface discovery task failed".into()))?
                .map(|found| json!(found))
                .map_err(|error| fdo::Error::Failed(error.into()));
            }
            _ => {}
        }
        let (epoch, was_active) = {
            let mut core = self.core.lock().unwrap();
            let active = core.status.active;
            let epoch = core
                .begin(method)
                .map_err(|s| fdo::Error::Failed(s.into()))?;
            (epoch, active)
        };
        // Broadcast the transition before the operation runs. The pinned daemon
        // emits connecting/stopping from its dispatcher so every tray or settings
        // client shows the change immediately, not at the next poll.
        if matches!(
            method,
            Method::Start | Method::Stop | Method::SwitchBandAndReconnect
        ) {
            self.emit_status(connection).await;
        }
        let mut startup_config = None;
        let mut resume_guard = None;
        if method == Method::Start && !was_active {
            self.power.lock().unwrap().cancel();
        }
        let power_revision = self.power.lock().unwrap().revision;
        if method == Method::Start {
            // Credentials and radio permissions must be fresh at every startup.
            let config = match self.load_config().await {
                Ok(config) => config,
                Err(error) => {
                    self.core.lock().unwrap().complete(method, epoch, false);
                    self.emit_status(connection).await;
                    return Err(error);
                }
            };
            if !self.core.lock().unwrap().is_current(epoch) {
                return Ok(json!({"success":false,"error":"Startup cancelled"}));
            }
            if !was_active {
                let inspection = tokio::select! {
                    result = self.radio.inspect(&config["WIFI_IFACE"]) => result.and_then(|snapshot| snapshot.preflight(&config)),
                    _ = cancelled.changed() => return Ok(json!({"success":false,"error":"Startup cancelled"})),
                };
                if !self.core.lock().unwrap().is_current(epoch) {
                    return Ok(json!({"success":false,"error":"Startup cancelled"}));
                }
                if let Err(error) = inspection {
                    {
                        let mut core = self.core.lock().unwrap();
                        if !core.is_current(epoch) {
                            return Ok(json!({"success":false,"error":"Startup cancelled"}));
                        }
                        core.complete(method, epoch, false);
                        core.status.error = Some(error.into());
                    }
                    self.emit_status(connection).await;
                    return Ok(json!({"success":false,"error":error}));
                }
            }
            if self.native.is_some() && !was_active && self.power.lock().unwrap().available {
                resume_guard = tokio::select! {
                    result = timeout(Duration::from_secs(8), networkmanager::resume_probe(self.fixture, &config)) => result.ok().and_then(Result::ok),
                    _ = cancelled.changed() => return Ok(json!({"success":false,"error":"Startup cancelled"})),
                };
            }
            startup_config = Some(config);
        }
        if method == Method::Start && self.power.lock().unwrap().blocked() {
            self.core.lock().unwrap().complete(method, epoch, false);
            return Ok(json!({"success":false,"error":"Startup cancelled for sleep"}));
        }
        let result = if let Some(native) = &self.native {
            match method {
                Method::Start => Ok(native
                    .start(startup_config.as_ref().unwrap(), cancelled.clone(), None)
                    .await),
                Method::Stop => Ok(json!(native.stop().await)),
                _ => Err("Unsupported native operation".into()),
            }
        } else {
            match &self.worker {
                Some(worker) => worker.call(method, params).await,
                None => self.demo(method, params, connection).await,
            }
        };
        match result {
            Ok(value) => {
                if let Err(error) = validate_reply(method, &value) {
                    self.core.lock().unwrap().backend_lost();
                    self.emit_status(connection).await;
                    return Err(fdo::Error::Failed(error.into()));
                }
                let success = value
                    .as_bool()
                    .or_else(|| value.get("success").and_then(Value::as_bool))
                    .unwrap_or(true);
                {
                    let mut core = self.core.lock().unwrap();
                    if let Some(status) = value.get("status").filter(|_| core.is_current(epoch)) {
                        let _ = core.observe(status.clone());
                    }
                    core.complete(method, epoch, success);
                    if self.native.is_some() && !success && core.is_current(epoch) {
                        if let Some(error) = value.get("error").and_then(Value::as_str) {
                            core.status.error = Some(error.into());
                        }
                    }
                }
                if method == Method::Start && success {
                    if let Some(original) = resume_guard {
                        // Arm only if the connection is still exactly the one authorized
                        // before startup; failed observations never grant resume intent.
                        let fresh = tokio::select! {
                            result = timeout(Duration::from_secs(8), networkmanager::resume_probe(self.fixture, startup_config.as_ref().unwrap())) => result.ok().and_then(Result::ok),
                            _ = cancelled.changed() => None,
                        };
                        if let Some(fresh) = fresh {
                            if fresh == original && self.core.lock().unwrap().is_current(epoch) {
                                self.power.lock().unwrap().arm(
                                    startup_config.as_ref().unwrap().clone(),
                                    original,
                                    power_revision,
                                );
                            }
                        }
                    }
                }
                if method == Method::Start
                    && (*cancelled.borrow() != request_revision
                        || !self.core.lock().unwrap().is_current(epoch))
                {
                    self.emit_status(connection).await;
                    return Ok(json!({"success":false,"error":"Startup cancelled"}));
                }
                if method == Method::Stop && success {
                    {
                        let core = self.core.lock().unwrap();
                        if core.is_current(epoch) {
                            *self.clients.lock().unwrap() = json!([]);
                        }
                    }
                    self.emit_clients(connection).await;
                }
                if matches!(
                    method,
                    Method::Start | Method::Stop | Method::SwitchBandAndReconnect
                ) {
                    self.emit_status(connection).await;
                    self.announce_error(connection).await;
                }
                Ok(value)
            }
            Err(error) => {
                if error == "Networking worker rejected the operation" {
                    self.core.lock().unwrap().complete(method, epoch, false);
                } else {
                    self.core.lock().unwrap().backend_lost();
                }
                self.emit_status(connection).await;
                Err(fdo::Error::Failed(
                    "Networking operation failed; inspect worker diagnostics and cleanup.".into(),
                ))
            }
        }
    }

    async fn load_config(&self) -> fdo::Result<Config> {
        let store = self.configuration.clone();
        tokio::task::spawn_blocking(move || store.lock().unwrap().load())
            .await
            .map_err(|_| fdo::Error::Failed("Configuration read task failed".into()))?
            .map_err(|error| fdo::Error::Failed(error.into()))
    }

    async fn save_config(&self, params: Value) -> fdo::Result<()> {
        let patch: Config = serde_json::from_value(params)
            .map_err(|_| fdo::Error::InvalidArgs("Expected string configuration values".into()))?;
        let current = self.load_config().await?;
        let merged = config::merge(&current, &patch)
            .map_err(|error| fdo::Error::InvalidArgs(error.into()))?;
        if let Some(native) = &self.native {
            if merged["BACKEND"] != native.name() {
                return Err(fdo::Error::InvalidArgs(format!("This native service mode requires BACKEND={}; switch service modes after confirmed cleanup", native.name())));
            }
        }
        let switching = current["BACKEND"] != merged["BACKEND"];
        if switching {
            let idle = {
                let core = self.core.lock().unwrap();
                !core.status.active
                    && !core.status.desired_active
                    && !matches!(
                        core.status.state.as_str(),
                        "connecting" | "stopping" | "failed"
                    )
            };
            if !idle {
                return Err(fdo::Error::Failed(
                    "Stop the hotspot before changing its backend".into(),
                ));
            }
            if let Some(worker) = &self.worker {
                // Fresh ownership/status inspection runs in the legacy operation queue.
                // The Rust gate prevents a new Start until this transaction commits.
                let observed = worker
                    .call(Method::GetStatus, Value::Null)
                    .await
                    .map_err(|_| {
                        fdo::Error::Failed(
                            "Cannot confirm that the networking backend is stopped".into(),
                        )
                    })?;
                validate_reply(Method::GetStatus, &observed)
                    .map_err(|e| fdo::Error::Failed(e.into()))?;
                let status: Status = serde_json::from_value(observed).unwrap();
                if status.active
                    || status.desired_active
                    || status.error.is_some()
                    || matches!(status.state.as_str(), "connecting" | "stopping" | "failed")
                {
                    return Err(fdo::Error::Failed(
                        "Stop the hotspot before changing its backend".into(),
                    ));
                }
            }
        }
        let changed = current != merged;
        let store = self.configuration.clone();
        tokio::task::spawn_blocking(move || store.lock().unwrap().save(&patch, switching))
            .await
            .map_err(|_| fdo::Error::Failed("Configuration save task failed".into()))?
            .map_err(|error| fdo::Error::Failed(error.into()))?;
        if changed {
            self.power.lock().unwrap().cancel();
            let mut core = self.core.lock().unwrap();
            if !core.status.active {
                core.status.desired_active = false;
            }
        }
        Ok(())
    }

    async fn demo(
        &self,
        method: Method,
        _params: Value,
        connection: &Connection,
    ) -> Result<Value, String> {
        use std::sync::atomic::Ordering;
        match method {
            Method::Start => {
                let generation = self.demo_generation.load(Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(400)).await;
                if generation != self.demo_generation.load(Ordering::SeqCst) {
                    return Ok(json!({"success":false,"error":"Startup cancelled"}));
                }
                let config = self
                    .load_config()
                    .await
                    .map_err(|_| "Cannot read demo configuration")?;
                let status = Status {
                    active: true,
                    desired_active: true,
                    state: "active".into(),
                    ssid: config["SSID"].clone(),
                    iface: "demo0".into(),
                    backend: "demo".into(),
                    band: "2.4".into(),
                    ..Status::default()
                };
                Ok(json!({"success":true,"status":status}))
            }
            Method::Stop => {
                self.demo_generation.fetch_add(1, Ordering::SeqCst);
                Ok(json!(true))
            }
            Method::PrepareFirewall => Ok(json!(true)),
            Method::SwitchBandAndReconnect => {
                let _ = connection;
                Ok(json!({"success":false,"error":"Band switching is unavailable in demo mode"}))
            }
            _ => Err("Unsupported demo method".into()),
        }
    }
}

#[zbus::interface(name = "io.github.erhanzeyrek.WifiHotspot")]
impl Service {
    async fn get_status(&self) -> String {
        serde_json::to_string(&self.core.lock().unwrap().status).unwrap()
    }
    async fn get_clients(&self) -> String {
        self.clients.lock().unwrap().to_string()
    }
    async fn start(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<String> {
        let request = self.cancellation.subscribe();
        self.authorize(connection, &header, MANAGE_ACTION).await?;
        Ok(self
            .invoke_requested(Method::Start, Value::Null, connection, Some(request))
            .await?
            .to_string())
    }
    async fn stop(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<bool> {
        self.authorize(connection, &header, MANAGE_ACTION).await?;
        Ok(self
            .invoke(Method::Stop, Value::Null, connection)
            .await?
            .as_bool()
            .unwrap())
    }
    async fn get_config(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<String> {
        self.authorize(connection, &header, CREDENTIALS_ACTION).await?;
        Ok(self
            .invoke(Method::GetConfig, Value::Null, connection)
            .await?
            .to_string())
    }
    async fn set_config(
        &self,
        config_json: &str,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<bool> {
        self.authorize(connection, &header, CREDENTIALS_ACTION).await?;
        let config = parse_patch(config_json).map_err(|e| fdo::Error::InvalidArgs(e.into()))?;
        Ok(self
            .invoke(Method::SetConfig, json!(config), connection)
            .await?
            .as_bool()
            .unwrap())
    }
    async fn get_interfaces(
        &self,
        #[zbus(connection)] connection: &Connection,
    ) -> fdo::Result<String> {
        Ok(self
            .invoke(Method::GetInterfaces, Value::Null, connection)
            .await?
            .to_string())
    }
    async fn get_capabilities(
        &self,
        #[zbus(connection)] connection: &Connection,
    ) -> fdo::Result<String> {
        Ok(self
            .invoke(Method::GetCapabilities, Value::Null, connection)
            .await?
            .to_string())
    }
    async fn switch_band_and_reconnect(
        &self,
        target_band: &str,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<String> {
        self.authorize(connection, &header, MANAGE_ACTION).await?;
        if !matches!(target_band, "2.4" | "5") {
            return Err(fdo::Error::InvalidArgs("Band must be 2.4 or 5".into()));
        }
        Ok(self
            .invoke(
                Method::SwitchBandAndReconnect,
                json!(target_band),
                connection,
            )
            .await?
            .to_string())
    }
    async fn prepare_firewall(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<bool> {
        self.authorize(connection, &header, MANAGE_ACTION).await?;
        Ok(self
            .invoke(Method::PrepareFirewall, Value::Null, connection)
            .await?
            .as_bool()
            .unwrap())
    }
    #[zbus(signal)]
    async fn status_changed(context: &SignalContext<'_>, status_json: &str) -> zbus::Result<()>;
    #[zbus(signal)]
    async fn clients_changed(context: &SignalContext<'_>, clients_json: &str) -> zbus::Result<()>;
    #[zbus(signal)]
    async fn user_action_required(
        context: &SignalContext<'_>,
        action_json: &str,
    ) -> zbus::Result<()>;
}

fn verify_tree(root: &Path) -> Result<(), Box<dyn std::error::Error>> {
    use std::os::unix::fs::MetadataExt;
    // Root executes only a root-owned installation, including all ancestor directories.
    for path in root.ancestors() {
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_dir() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
            return Err("Unsafe installation directory".into());
        }
    }
    let mut directories = vec![root.to_path_buf()];
    while let Some(path) = directories.pop() {
        for entry in std::fs::read_dir(path)? {
            let entry = entry?;
            let metadata = entry.path().symlink_metadata()?;
            if metadata.uid() != 0
                || metadata.mode() & 0o022 != 0
                || metadata.file_type().is_symlink()
                || !(metadata.is_file() || metadata.is_dir())
            {
                return Err("Unsafe installation payload".into());
            }
            if metadata.is_dir() {
                directories.push(entry.path());
            }
        }
    }
    Ok(())
}

/// Create a fresh private configuration. `--detect-interface` adopts the only
/// Wi-Fi adapter, mirroring the pinned installer's unambiguous-device rule; without
/// it the documented `wlan0` default is kept so image assembly cannot capture the
/// build machine's hardware.
fn init_config(arguments: &[String], net: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let path = arguments
        .first()
        .ok_or("--init-config requires an absolute path")?;
    let mut ssid = "Hotspot";
    let mut interface: Option<String> = None;
    let mut initial_backend = "create_ap";
    let mut detect = false;
    let mut options = arguments[1..].iter();
    while let Some(option) = options.next() {
        match option.as_str() {
            "--ssid" => ssid = options.next().ok_or("--ssid requires a value")?,
            "--wifi-interface" => {
                interface = Some(
                    options
                        .next()
                        .ok_or("--wifi-interface requires a value")?
                        .clone(),
                )
            }
            "--backend" => initial_backend = options.next().ok_or("--backend requires a value")?,
            "--detect-interface" => detect = true,
            _ => return Err("Unknown configuration initialization option".into()),
        }
    }
    config::parse_patch(&json!({"BACKEND": initial_backend}).to_string())?;
    let interface = match interface {
        Some(interface) => Some(interface),
        None if detect => {
            let detected = inventory::unique_wifi(net);
            if detected.is_none() {
                eprintln!(
                    "No unambiguous Wi-Fi adapter detected; keeping the default interface"
                );
            }
            detected
        }
        None => None,
    };
    // SAFETY: geteuid has no preconditions.
    let file = config_store::ConfigFile::open(Path::new(path), unsafe { libc::geteuid() })?;
    let created = file.initialize(ssid, interface.as_deref())?;
    if created && initial_backend != "create_ap" {
        config_store::Store::Disk(file).save(
            &Config::from([("BACKEND".into(), initial_backend.into())]),
            true,
        )?;
    }
    eprintln!(
        "{}",
        if created {
            "Initialized private configuration"
        } else {
            "Configuration already exists; left unchanged"
        }
    );
    Ok(())
}

/// Package-upgrade migration: replace an obsolete `wlan0` assignment when the
/// legacy interface is absent and exactly one Wi-Fi adapter (or the explicit
/// `--wifi-interface`) can own it. Everything else in the file is preserved.
fn repair_config_interface(
    arguments: &[String],
    net: &Path,
) -> Result<(), Box<dyn std::error::Error>> {
    let path = arguments
        .first()
        .ok_or("--repair-config-interface requires an absolute path")?;
    let mut interface: Option<String> = None;
    let mut options = arguments[1..].iter();
    while let Some(option) = options.next() {
        match option.as_str() {
            "--wifi-interface" => {
                interface = Some(
                    options
                        .next()
                        .ok_or("--wifi-interface requires a value")?
                        .clone(),
                )
            }
            _ => return Err("Unknown interface repair option".into()),
        }
    }
    // SAFETY: geteuid has no preconditions.
    let file = config_store::ConfigFile::open(Path::new(path), unsafe { libc::geteuid() })?;
    let repaired = file.repair_interface(interface.as_deref(), net)?;
    eprintln!(
        "{}",
        if repaired {
            "Repaired the obsolete wlan0 interface default"
        } else {
            "Interface defaults left unchanged"
        }
    );
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--deployment-protocol"] {
        println!("{}", deployment::live::PROTOCOL);
        return Ok(());
    }
    if let Some(mode @ ("--deploy-live" | "--test-deploy-live")) = args.first().map(String::as_str)
    {
        return deployment::live::run(&args[1..], mode == "--test-deploy-live")
            .await
            .map_err(Into::into);
    }
    if let Some(mode @ ("--deploy" | "--test-deploy")) = args.first().map(String::as_str) {
        return deployment::run(&args[1..], mode == "--test-deploy").map_err(Into::into);
    }
    if let Some(mode @ ("--create-ap-instance" | "--test-ap-instance")) =
        args.first().map(String::as_str)
    {
        return ap_runtime::run_instance(&args[1..], mode == "--test-ap-instance")
            .await
            .map_err(Into::into);
    }
    if args.first().map(String::as_str) == Some("--create-ap-exec") {
        return create_ap::exec_gate(&args);
    }
    if let Some(mode) = args.first().map(String::as_str) {
        if matches!(mode, "--create-ap-runtime" | "--test-ap-runtime") {
            match ap_runtime::run(&args[1..], mode == "--test-ap-runtime").await {
                Ok(output) => {
                    if !output.is_empty() {
                        println!("{output}");
                    }
                    return Ok(());
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
    // Diagnostic modes return before bus registration or worker construction.
    // They run read-only iw inspection and never apply a startup plan.
    if let [mode, target] = args.as_slice() {
        if mode == "--inspect-radio" {
            let snapshot = radio::Source::System.inspect(target).await?;
            println!("{}", serde_json::to_string(&snapshot.capabilities)?);
            return Ok(());
        }
        if mode == "--preflight-radio" {
            // SAFETY: geteuid has no preconditions.
            let config = config_store::Store::Disk(config_store::ConfigFile::open(
                Path::new(target),
                unsafe { libc::geteuid() },
            )?)
            .load()?;
            let snapshot = radio::Source::System.inspect(&config["WIFI_IFACE"]).await?;
            let result = match snapshot.preflight(&config) {
                Ok(plan) => json!({"success":true,"plan":plan}),
                Err(error) => json!({"success":false,"error":error}),
            };
            println!("{result}");
            return Ok(());
        }
    }
    if let Some(mode @ ("--init-config" | "--test-init-config")) =
        args.first().map(String::as_str)
    {
        // The test variant selects the interface inventory root instead of sysfs.
        let (net, arguments) = if mode == "--test-init-config" {
            (
                Path::new(args.get(1).ok_or("--test-init-config requires a net root")?),
                &args[2..],
            )
        } else {
            (Path::new("/sys/class/net"), &args[1..])
        };
        return init_config(arguments, net);
    }
    if let Some(mode @ ("--repair-config-interface" | "--test-repair-config-interface")) =
        args.first().map(String::as_str)
    {
        let (net, arguments) = if mode == "--test-repair-config-interface" {
            (
                Path::new(
                    args.get(1)
                        .ok_or("--test-repair-config-interface requires a net root")?,
                ),
                &args[2..],
            )
        } else {
            (Path::new("/sys/class/net"), &args[1..])
        };
        return repair_config_interface(arguments, net);
    }
    // ExecStopPost teardown calls the binary again with --recover-all: the same
    // native mode selection runs journal recovery without the live D-Bus daemon.
    let mut recover_only = false;
    let args: Vec<String> = args
        .into_iter()
        .filter(|argument| {
            if argument == "--recover-all" {
                recover_only = true;
                false
            } else {
                true
            }
        })
        .collect();
    let (demo,deny,use_polkit,root,config_path)=match args.as_slice() {
        [mode,flag,path] if mode=="--native-create-ap" && flag=="--legacy-root" => (false,false,true,Some(PathBuf::from(path)),Some(PathBuf::from("/etc/wifi-hotspot.conf"))),
        [mode,_capture,path,_state,_helper] if mode=="--test-create-ap" => (true,false,false,None,Some(PathBuf::from(path))),
        [mode] if mode=="--native-networkmanager" => (false,false,true,None,Some(PathBuf::from("/etc/wifi-hotspot.conf"))),
        [mode,_capture,path,_state] if mode=="--test-networkmanager" => (true,false,false,None,Some(PathBuf::from(path))),
        [mode] if mode=="--demo" => (true,false,false,None,None),
        [mode] if mode=="--demo-maintenance" => (true,false,false,None,None),
        [mode] if mode=="--demo-deny" => (true,true,false,None,None),
        [mode] if mode=="--demo-polkit" => (true,false,true,None,None),
        [mode,_path] if mode=="--demo-radio" => (true,false,false,None,None),
        [mode,path] if mode=="--demo-config" => (true,false,false,None,Some(PathBuf::from(path))),
        [mode,flag,path] if mode=="--system" && flag=="--legacy-root" => (false,false,true,Some(PathBuf::from(path)),Some(PathBuf::from("/etc/wifi-hotspot.conf"))),
        _=>return Err("Usage: relayforge-service --native-networkmanager | --native-create-ap --legacy-root ROOT | --recover-all | --inspect-radio IFACE | --preflight-radio PATH | --demo | --demo-deny | --demo-polkit | --demo-config PATH | --demo-radio FIXTURE | --init-config PATH [--ssid NAME] [--wifi-interface NAME] [--backend create_ap|networkmanager] [--detect-interface] | --repair-config-interface PATH [--wifi-interface NAME] | --system --legacy-root /root-owned/installation".into()),
    };
    let native_mode = matches!(
        args.first().map(String::as_str),
        Some(
            "--native-networkmanager"
                | "--test-networkmanager"
                | "--native-create-ap"
                | "--test-create-ap"
        )
    );
    let create_ap_mode = matches!(
        args.first().map(String::as_str),
        Some("--native-create-ap" | "--test-create-ap")
    );
    let fixture = matches!(
        args.first().map(String::as_str),
        Some("--test-networkmanager" | "--test-create-ap")
    );
    if recover_only && !native_mode {
        return Err("--recover-all is a native service mode option".into());
    }
    if fixture && unsafe { libc::geteuid() } == 0 {
        return Err("Network fixtures must run as an ordinary user".into());
    }
    let radio = if args.first().map(String::as_str) == Some("--demo-radio") || fixture {
        use std::io::Read;
        let mut bytes = Vec::new();
        std::fs::File::open(&args[1])?
            .take((4 * relayforge_core::radio::MAX_CAPTURE + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 4 * relayforge_core::radio::MAX_CAPTURE {
            return Err("Radio replay exceeds limit".into());
        }
        radio::Source::Replay(Box::new(serde_json::from_slice(&bytes)?))
    } else if demo {
        radio::Source::Demo
    } else {
        radio::Source::System
    };
    if !demo {
        // SAFETY: geteuid has no preconditions.
        if unsafe { libc::geteuid() } != 0 {
            return Err("System mode requires root".into());
        }
        if let Some(root) = &root {
            verify_tree(root)?;
        }
        // Teardown recovery must run even when compatibility residue or a broken
        // world is present; it only ever touches resources its journal owns.
        if native_mode
            && !recover_only
            && [
                "/run/wifi-relay/nm-owned.json",
                "/run/wifi-relay/create-ap.pid",
            ]
            .iter()
            .any(|p| Path::new(p).exists())
        {
            return Err(
                "Stop and clean up the existing compatibility backend before using native mode"
                    .into(),
            );
        }
    }
    let configuration = if recover_only {
        // Recovery deliberately works without credentials or a readable config.
        config_store::Store::Memory(config::defaults())
    } else if let Some(path) = config_path {
        // SAFETY: geteuid has no preconditions.
        let store = config_store::Store::Disk(config_store::ConfigFile::open(&path, unsafe {
            libc::geteuid()
        })?);
        // A fresh installation has no configuration. Name the path and the remedy, and
        // exit with a status the packaged unit is told never to restart: otherwise a
        // desktop client polling GetStatus re-triggers D-Bus activation forever.
        match store.load() {
            Ok(_) => {}
            Err(config_store::MISSING) => {
                eprintln!(
                    "RelayForge: {} is missing. Create it before sharing, for example:\n  \
                     {} --init-config {} --wifi-interface wlan0",
                    path.display(),
                    std::env::current_exe()
                        .map(|v| v.display().to_string())
                        .unwrap_or_else(|_| "relayforge-service".into()),
                    path.display()
                );
                std::process::exit(EXIT_MISSING_CONFIG);
            }
            Err(error) => return Err(error.into()),
        }
        store
    } else {
        let config = config::merge(
            &config::defaults(),
            &Config::from([
                ("SSID".into(), "RelayForge Demo".into()),
                ("PASSPHRASE".into(), "demo-only-password".into()),
                ("WIFI_IFACE".into(), "demo0".into()),
                ("INTERNET_IFACE".into(), "demo1".into()),
            ]),
        )?;
        config_store::Store::Memory(config)
    };
    let native = if native_mode {
        let expected = if create_ap_mode {
            "create_ap"
        } else {
            "networkmanager"
        };
        if !recover_only && configuration.load()?["BACKEND"] != expected {
            return Err(format!("Native service requires BACKEND={expected}").into());
        }
        let path = if fixture {
            PathBuf::from(&args[3])
        } else {
            PathBuf::from("/run/relayforge")
        };
        if !fixture && !path.exists() {
            use std::os::unix::fs::DirBuilderExt;
            std::fs::DirBuilder::new().mode(0o700).create(&path)?;
        }
        let backend = if create_ap_mode {
            let helper = if fixture {
                PathBuf::from(&args[4])
            } else {
                root.as_ref().unwrap().join("native/create_ap")
            };
            native_backend::Backend::CreateAp(create_ap::Backend::new(
                create_ap::Platform {
                    fixture,
                    runtime: path,
                },
                &helper,
            )?)
        } else {
            native_backend::Backend::NetworkManager(networkmanager::Backend::new(
                networkmanager::Platform { fixture },
                nm_journal::Journal::open(&path, unsafe { libc::geteuid() })?,
            ))
        };
        if recover_only {
            // The pinned daemon's ExecStopPost runs nm_backend.py --recover-all:
            // release only journal-owned resources, then let systemd continue.
            let recovered = backend.recover().await;
            match &recovered {
                Ok(()) => eprintln!("RelayForge: recovered owned sharing resources"),
                Err(error) => eprintln!("RelayForge: recovery incomplete: {error}"),
            }
            std::process::exit(if recovered.is_ok() { 0 } else { 1 });
        }
        Some(Arc::new(backend))
    } else {
        None
    };
    let builder = if demo {
        zbus::connection::Builder::session()?
    } else {
        zbus::connection::Builder::system()?
    };
    let connection = builder.serve_at(OBJECT_PATH, Initializing)?.build().await?;
    let reply = connection
        .request_name_with_flags(BUS_NAME, fdo::RequestNameFlags::DoNotQueue.into())
        .await?;
    if reply != fdo::RequestNameReply::PrimaryOwner {
        return Err("Existing Wi-Fi Relay service owns the compatibility name".into());
    }
    if let Some(native) = &native {
        native.recover().await?;
    }
    let core = Arc::new(Mutex::new(Lifecycle::default()));
    let clients = Arc::new(Mutex::new(json!([])));
    let (events, mut receiver) = mpsc::channel::<backend::Event>(64);
    let worker = if native_mode {
        None
    } else if let Some(root) = root {
        Some(Worker::spawn(&root, events).await?)
    } else {
        None
    };
    let (cancellation, _) = tokio::sync::watch::channel(0);
    let service = Service {
        core: core.clone(),
        clients: clients.clone(),
        worker: worker.clone(),
        native: native.clone(),
        configuration: Arc::new(Mutex::new(configuration)),
        operations: tokio::sync::Mutex::new(()),
        notifications: tokio::sync::Mutex::new(()),
        config_users: tokio::sync::RwLock::new(()),
        radio,
        cancellation: cancellation.clone(),
        power: Mutex::new(power::State::default()),
        maintenance: std::sync::atomic::AtomicBool::new(
            args.first().map(String::as_str) == Some("--demo-maintenance")
                || !demo
                    && match std::fs::symlink_metadata("/var/lib/relayforge-deploy/live.json") {
                        Ok(_) => true,
                        Err(error) => error.kind() != std::io::ErrorKind::NotFound,
                    },
        ),
        fixture,
        demo,
        demo_deny: deny,
        use_polkit,
        demo_generation: std::sync::atomic::AtomicU64::new(0),
        reported_error: Mutex::new(None),
        requests: Semaphore::new(31),
        stop_requests: Semaphore::new(1),
    };
    // Complete the first logind snapshot/inhibitor handshake before accepting Start.
    let initial_power = if native_mode {
        power::initialize(&connection, &service).await
    } else {
        None
    };
    connection.object_server().at(OBJECT_PATH, service).await?;
    connection
        .object_server()
        .at(
            deployment::live::GATE_PATH,
            deployment::live::Gate {
                uid: if demo { unsafe { libc::geteuid() } } else { 0 },
            },
        )
        .await?;
    connection
        .object_server()
        .remove::<Initializing, _>(OBJECT_PATH)
        .await?;
    let power_task = if native_mode {
        Some(tokio::spawn(power::run(connection.clone(), initial_power)))
    } else {
        None
    };
    let native_monitor = native.clone().map(|backend| {
        let core = core.clone();
        let clients = clients.clone();
        let connection = connection.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_millis(500)).await;
                let interface = match connection
                    .object_server()
                    .interface::<_, Service>(OBJECT_PATH)
                    .await
                {
                    Ok(interface) => interface,
                    Err(_) => return,
                };
                let service = interface.get().await;
                if service.power.lock().unwrap().owns_status() {
                    continue;
                }
                let epoch = core.lock().unwrap().epoch();
                if let Some((status, observed)) = backend.poll().await {
                    let changed = {
                        let mut power = service.power.lock().unwrap();
                        let mut core = core.lock().unwrap();
                        let mut clients = clients.lock().unwrap();
                        power.observe(&mut core, epoch, status, &mut clients, observed)
                    };
                    if let Some(changed) = changed {
                        service.emit_status(&connection).await;
                        if changed {
                            service.emit_clients(&connection).await;
                        }
                        service.announce_error(&connection).await;
                    }
                }
            }
        })
    });
    let radio_task = if native_mode {
        Some(tokio::spawn(radio_watch::run(connection.clone())))
    } else {
        None
    };
    let event_connection = connection.clone();
    let shutdown_core = core.clone();
    let failed_worker = worker.clone();
    let event_task = tokio::spawn(async move {
        while let Some((event, ack)) = receiver.recv().await {
            if event.get("lost").and_then(Value::as_bool) == Some(true) {
                if let Some(worker) = failed_worker.clone() {
                    tokio::spawn(async move {
                        if worker.close_and_wait().await.is_err() {
                            eprintln!("Worker cleanup unconfirmed after transport failure");
                        }
                    });
                }
            }
            let name = event.get("signal").and_then(Value::as_str).unwrap_or("");
            let data = event.get("data").cloned().unwrap_or(Value::Null);
            let message = match name {
                "StatusChanged" => {
                    let mut state = core.lock().unwrap();
                    if state.observe(data).is_err() {
                        state.backend_lost();
                    }
                    Some((
                        "StatusChanged",
                        serde_json::to_string(&state.status).unwrap(),
                    ))
                }
                "ClientsChanged" if validate_reply(Method::GetClients, &data).is_ok() => {
                    *clients.lock().unwrap() = data.clone();
                    Some(("ClientsChanged", data.to_string()))
                }
                "UserActionRequired"
                    if data.get("type").is_some_and(Value::is_string)
                        && data.get("message").is_some_and(Value::is_string) =>
                {
                    Some(("UserActionRequired", data.to_string()))
                }
                _ => {
                    core.lock().unwrap().backend_lost();
                    Some((
                        "StatusChanged",
                        serde_json::to_string(&core.lock().unwrap().status).unwrap(),
                    ))
                }
            };
            if let Some((name, body)) = message {
                let _ = event_connection
                    .emit_signal(None::<&str>, OBJECT_PATH, BUS_NAME, name, &(body,))
                    .await;
            }
            let _ = ack.send(());
        }
    });
    eprintln!(
        "RelayForge ready ({})",
        if demo {
            "demo: no networking"
        } else if native_mode {
            "native sharing backend"
        } else {
            "compatibility backend"
        }
    );
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! { _=tokio::signal::ctrl_c()=>{}, _=terminate.recv()=>{} }
    cancellation.send_modify(|revision| *revision = revision.wrapping_add(1));
    if let Some(task) = power_task {
        task.abort();
        let _ = task.await;
    }
    if let Some(task) = native_monitor {
        task.abort();
        let _ = task.await;
    }
    if let Some(task) = radio_task {
        task.abort();
        let _ = task.await;
    }
    if let Some(native) = native {
        if !native.stop().await {
            return Err("Native shutdown cleanup unconfirmed; journal retained".into());
        }
    }
    shutdown_core.lock().unwrap().backend_lost();
    if let Some(worker) = worker {
        worker.shutdown().await?;
    }
    event_task.abort();
    Ok(())
}
