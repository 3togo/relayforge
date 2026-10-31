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
    validate_reply, Lifecycle, Method, Status, BUS_NAME, CONTROL_ACTION, MANAGE_ACTION,
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
    worker: Arc<std::sync::OnceLock<Option<Arc<Worker>>>>,
    ready: tokio::sync::watch::Receiver<bool>,
    native: Option<Arc<native_backend::Backend>>,
    band_switch: Option<Arc<band_switch::Backend>>,
    allow_band_switch: bool,
    allow_existing_bridge: bool,
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

impl Service {
    async fn wait_ready(&self) -> fdo::Result<()> {
        let mut ready = self.ready.clone();
        timeout(Duration::from_secs(90), ready.wait_for(|ready| *ready))
            .await
            .map_err(|_| fdo::Error::Failed("Service initialization timed out".into()))?
            .map_err(|_| fdo::Error::Failed("Service initialization failed".into()))?;
        Ok(())
    }

    async fn authorize(
        &self,
        connection: &Connection,
        header: &Header<'_>,
        action: &str,
    ) -> fdo::Result<()> {
        self.wait_ready().await?;
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
                        u32::from(
                            header
                                .primary()
                                .flags()
                                .contains(zbus::message::Flags::AllowInteractiveAuth),
                        ),
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
        self.wait_ready().await?;
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
        if matches!(method, Method::Start | Method::SwitchBandAndReconnect)
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
            if matches!(method, Method::Start | Method::SetConfig)
                && self.band_switch.as_ref().unwrap().pending().await
            {
                return Err(fdo::Error::Failed(
                    "Band-switch recovery is pending; inspect the upstream before retrying".into(),
                ));
            }
            if method == Method::Start && self.power.lock().unwrap().blocked() {
                return Ok(
                    json!({"success":false,"error":"Sharing is suspended or sleep cleanup is pending"}),
                );
            }
            match method {
                Method::SwitchBandAndReconnect => {
                    if !self.allow_band_switch {
                        return Ok(
                            json!({"success":false,"error":"Native sharing preserves the existing upstream; reconnect Wi-Fi explicitly before Start. Experimental band switching requires the administrator's --allow-band-switch opt-in"}),
                        );
                    }
                    if self.power.lock().unwrap().blocked() {
                        return Ok(
                            json!({"success":false,"error":"Band switching is blocked during sleep or pending cleanup"}),
                        );
                    }
                    {
                        let core = self.core.lock().unwrap();
                        if core.status.active || core.status.desired_active {
                            return Ok(
                                json!({"success":false,"error":"Stop sharing before explicitly switching the upstream band"}),
                            );
                        }
                    }
                    self.power.lock().unwrap().cancel();
                    let config = self.load_config().await?;
                    let epoch = self
                        .core
                        .lock()
                        .unwrap()
                        .begin(method)
                        .map_err(|e| fdo::Error::Failed(e.into()))?;
                    self.emit_status(connection).await;
                    let value = self
                        .band_switch
                        .as_ref()
                        .unwrap()
                        .switch(&config, params.as_str().unwrap_or(""), cancelled.clone())
                        .await;
                    let success = value["success"] == true;
                    {
                        let mut core = self.core.lock().unwrap();
                        if core.is_current(epoch) {
                            let _ = core.observe(json!(Status {
                                state: "idle".into(),
                                ..Status::default()
                            }));
                            core.complete(method, epoch, success);
                            if !success {
                                core.status.error = value["error"].as_str().map(str::to_owned);
                            }
                        }
                    }
                    self.emit_status(connection).await;
                    return Ok(value);
                }
                Method::PrepareFirewall => return Ok(json!(true)),
                _ => {}
            }
        }
        match method {
            Method::GetCapabilities => {
                let config = self.load_config().await?;
                let mut native_driver = None;
                let snapshot = self
                    .radio
                    .inspect(&config["WIFI_IFACE"])
                    .await
                    .map_err(|error| fdo::Error::Failed(error.into()))?;
                let preflight = if let Some(native) = &self.native {
                    let mut result = snapshot
                        .native_preflight_with_bridge(&config, self.allow_existing_bridge)
                        .map(|_| ());
                    if result.is_ok() {
                        match native.driver_preflight(&config) {
                            Ok(module) => native_driver = module,
                            Err(error) => result = Err(error),
                        }
                    }
                    if result.is_ok() && config["SHARE_METHOD"] == "bridge" {
                        result = native.bridge_preflight(&config).await;
                    }
                    if result.is_ok() {
                        result = native.environment_preflight(&config, snapshot.phy).await;
                    }
                    Some(match result {
                        Ok(_) => json!({"success":true,"scope":"configuration-and-radio"}),
                        Err(error) => {
                            json!({"success":false,"scope":"configuration-and-radio","error":error})
                        }
                    })
                } else {
                    None
                };
                let mut capabilities = json!(snapshot.capabilities);
                if let Some(module) = native_driver {
                    capabilities["native_driver"] = json!({"kernel_module":module,"hostapd_interface":"nl80211","scope":"policy-only"});
                }
                if self
                    .native
                    .as_ref()
                    .is_some_and(|b| b.name() == "create_ap")
                {
                    // Security description is policy, never evidence of actual client negotiation.
                    if let Ok(security) = relayforge_core::ap_policy::security(&config) {
                        capabilities["native_security"] = json!(security);
                    }
                    capabilities["native_policy"] = json!({"country":"require-existing-radio-domain", "requested_country":config.get("COUNTRY"), "regulatory_mutation":false,
                        "firewall":"scoped-iptables-without-active-firewalld", "pptp_module_loading":false, "parent_mac_override":false});
                }
                if let Some(preflight) = preflight {
                    capabilities["startup_preflight"] = preflight;
                    capabilities["upstream_band_switch"] = json!({"enabled":self.allow_band_switch,
                        "profile_mutation":false,"pending_recovery":self.band_switch.as_ref().unwrap().pending().await});
                    capabilities["existing_bridge"] = json!({"enabled":self.allow_existing_bridge,"scope":"owned-virtual-child-only"});
                }
                return Ok(capabilities);
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
                Method::Stop => {
                    let clean = native.stop().await;
                    let band_clean = self.band_switch.as_ref().unwrap().recover().await.is_ok();
                    Ok(json!(clean && band_clean))
                }
                _ => Err("Unsupported native operation".into()),
            }
        } else {
            match self.worker.get().and_then(Option::as_ref) {
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
            if let Some(worker) = self.worker.get().and_then(Option::as_ref) {
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
    async fn get_status(&self) -> fdo::Result<String> {
        self.wait_ready().await?;
        Ok(serde_json::to_string(&self.core.lock().unwrap().status).unwrap())
    }
    async fn get_clients(&self) -> fdo::Result<String> {
        self.wait_ready().await?;
        Ok(self.clients.lock().unwrap().to_string())
    }
    async fn start(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<String> {
        let request = self.cancellation.subscribe();
        self.authorize(connection, &header, CONTROL_ACTION).await?;
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
        self.authorize(connection, &header, CONTROL_ACTION).await?;
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
        self.authorize(connection, &header, MANAGE_ACTION).await?;
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
        self.authorize(connection, &header, MANAGE_ACTION).await?;
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
        let request = self.cancellation.subscribe();
        self.authorize(connection, &header, MANAGE_ACTION).await?;
        if !matches!(target_band, "2.4" | "5") {
            return Err(fdo::Error::InvalidArgs("Band must be 2.4 or 5".into()));
        }
        Ok(self
            .invoke_requested(
                Method::SwitchBandAndReconnect,
                json!(target_band),
                connection,
                Some(request),
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
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut allow_existing_bridge = false;
    let mut allow_band_switch = false;
    let native_service = matches!(
        args.first().map(String::as_str),
        Some(
            "--native-create-ap"
                | "--test-create-ap"
                | "--native-networkmanager"
                | "--test-networkmanager"
        )
    );
    while let Some(flag) = args.last().map(String::as_str).filter(|_| native_service) {
        match flag {
            "--allow-existing-bridge" if !allow_existing_bridge => {
                allow_existing_bridge = true;
            }
            "--allow-band-switch" if !allow_band_switch => {
                allow_band_switch = true;
            }
            _ => break,
        }
        args.pop();
    }
    if allow_existing_bridge
        && !matches!(
            args.first().map(String::as_str),
            Some("--native-create-ap" | "--test-create-ap")
        )
    {
        return Err("--allow-existing-bridge requires a native create_ap service mode".into());
    }
    if allow_band_switch {
        if !matches!(
            args.first().map(String::as_str),
            Some(
                "--native-networkmanager"
                    | "--native-create-ap"
                    | "--test-networkmanager"
                    | "--test-create-ap"
            )
        ) {
            return Err("--allow-band-switch is valid only for a native service mode".into());
        }
    }
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
    if args.first().map(String::as_str) == Some("--migrate-config") {
        let path = args
            .get(1)
            .ok_or("--migrate-config requires an absolute path")?;
        let mut interface = None;
        let mut detect = false;
        let mut dry_run = false;
        let mut index = 2;
        while index < args.len() {
            match args[index].as_str() {
                "--dry-run" => dry_run = true,
                "--detect-interface" => detect = true,
                "--wifi-interface" => {
                    index += 1;
                    interface = Some(
                        args.get(index)
                            .ok_or("--wifi-interface requires a value")?
                            .clone(),
                    );
                }
                _ => return Err("Unknown legacy migration option".into()),
            }
            index += 1;
        }
        if detect && interface.is_some() {
            return Err("Choose explicit or detected interface repair, not both".into());
        }
        if detect || interface.is_some() {
            let devices = inventory::discover(Path::new("/sys/class/net"))?;
            if devices.all_interfaces.iter().any(|name| name == "wlan0") {
                return Err("Legacy wlan0 exists; interface repair is not permitted".into());
            }
            if detect {
                if devices.wifi_interfaces.len() != 1 {
                    return Err("Automatic repair requires exactly one Wi-Fi adapter".into());
                }
                interface = Some(devices.wifi_interfaces[0].clone());
            } else if !devices
                .wifi_interfaces
                .contains(interface.as_ref().unwrap())
            {
                return Err("The replacement must be an observed Wi-Fi adapter".into());
            }
        }
        // This command is for protected, offline configuration copies. Avoid
        // racing live Start/configuration transactions or changing credentials
        // underneath connected hotspot clients. A live dry-run remains read-only.
        if !dry_run
            && std::fs::canonicalize(path).ok().as_deref()
                == Some(Path::new("/etc/wifi-hotspot.conf"))
        {
            return Err("Migrate a protected offline configuration copy; live configuration writes are refused".into());
        }
        let file = config_store::ConfigFile::open(Path::new(path), unsafe { libc::geteuid() })?;
        let report = file.migrate_legacy(interface.as_deref(), dry_run)?;
        println!("{}", serde_json::to_string(&report)?);
        return Ok(());
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
        _=>return Err("Usage: relayforge-service --native-networkmanager | --native-create-ap --legacy-root ROOT | --recover-all | --inspect-radio IFACE | --preflight-radio PATH | --demo | --demo-deny | --demo-polkit | --demo-config PATH | --demo-radio FIXTURE | --migrate-config PATH [--dry-run] [--detect-interface | --wifi-interface NAME] | --init-config PATH [--ssid NAME] [--wifi-interface NAME] [--backend create_ap|networkmanager] [--detect-interface] | --repair-config-interface PATH [--wifi-interface NAME] | --system --legacy-root /root-owned/installation".into()),
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
    let mut band_switch = None;
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
        band_switch = Some(band_switch::Backend::open(&path, fixture)?);
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
                allow_existing_bridge,
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
            let band_recovered = band_switch.as_ref().unwrap().recover().await;
            let recovered = recovered.and(band_recovered);
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
    let core = Arc::new(Mutex::new(Lifecycle::default()));
    let clients = Arc::new(Mutex::new(json!([])));
    let (events, mut receiver) = mpsc::channel::<backend::Event>(64);
    // Register the complete interface before claiming the name. Calls wait for
    // recovery and the power handshake, while name ownership still excludes a
    // second supervisor throughout initialization.
    let worker_slot = Arc::new(std::sync::OnceLock::new());
    let (startup_complete, ready) = tokio::sync::watch::channel(false);
    let (cancellation, _) = tokio::sync::watch::channel(0);
    let service = Service {
        core: core.clone(),
        clients: clients.clone(),
        worker: worker_slot.clone(),
        ready,
        native: native.clone(),
        band_switch: band_switch.clone(),
        allow_band_switch,
        allow_existing_bridge,
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
    let builder = if demo {
        zbus::connection::Builder::session()?
    } else {
        zbus::connection::Builder::system()?
    };
    let connection = builder.serve_at(OBJECT_PATH, service)?.build().await?;
    let reply = connection
        .request_name_with_flags(BUS_NAME, fdo::RequestNameFlags::DoNotQueue.into())
        .await?;
    if reply != fdo::RequestNameReply::PrimaryOwner {
        return Err("Existing Wi-Fi Relay service owns the compatibility name".into());
    }
    if let Some(native) = &native {
        native.recover().await?;
    }
    if let Some(band) = &band_switch {
        band.recover().await?;
    }
    let worker = if native_mode {
        None
    } else if let Some(root) = root {
        Some(Worker::spawn(&root, events).await?)
    } else {
        None
    };
    worker_slot
        .set(worker.clone())
        .map_err(|_| "Worker initialized twice")?;
    let interface = connection
        .object_server()
        .interface::<_, Service>(OBJECT_PATH)
        .await?;
    let service = interface.get().await;
    // Complete the first logind snapshot/inhibitor handshake before accepting Start.
    let initial_power = if native_mode {
        power::initialize(&connection, &service).await
    } else {
        None
    };
    drop(service);
    drop(interface);
    connection
        .object_server()
        .at(
            deployment::live::GATE_PATH,
            deployment::live::Gate {
                uid: if demo { unsafe { libc::geteuid() } } else { 0 },
            },
        )
        .await?;
    startup_complete.send_replace(true);
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
