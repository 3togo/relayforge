//! Sharing operations and lifecycle state shared by desktop controls.
mod dbus;
mod power;
mod radio_watch;
mod runtime;

pub(crate) use runtime::run;

use crate::backend::Worker;
use crate::{backend, band_switch, config_store, inventory, native_backend, networkmanager, radio};
use relayforge_core::{
    config::{self, Config},
    validate_reply, Lifecycle, Method, Status, BUS_NAME, OBJECT_PATH,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::Path,
    sync::{Arc, Mutex},
};
use tokio::{
    sync::Semaphore,
    time::{timeout, Duration},
};
use zbus::{fdo, message::Header, zvariant::Value as BusValue, Connection};

pub(crate) struct Service {
    pub(crate) core: Arc<Mutex<Lifecycle>>,
    clients: Arc<Mutex<Value>>,
    worker: Arc<std::sync::OnceLock<Option<Arc<Worker>>>>,
    ready: tokio::sync::watch::Receiver<bool>,
    native: Option<Arc<native_backend::Backend>>,
    band_switch: Option<Arc<band_switch::Backend>>,
    allow_band_switch: bool,
    allow_existing_bridge: bool,
    configuration: Arc<Mutex<config_store::Store>>,
    pub(crate) operations: tokio::sync::Mutex<()>,
    notifications: tokio::sync::Mutex<()>,
    pub(crate) config_users: tokio::sync::RwLock<()>,
    radio: radio::Source,
    cancellation: tokio::sync::watch::Sender<u64>,
    pub(crate) power: Mutex<power::State>,
    /// Last backend error already published as `UserActionRequired`; native
    /// monitoring mirrors the pinned poll, which only announces a *changed* error.
    reported_error: Mutex<Option<String>>,
    pub(crate) maintenance: std::sync::atomic::AtomicBool,
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
