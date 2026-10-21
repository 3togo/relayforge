mod backend;
use backend::Worker;
use relayforge_core::{
    config::parse_patch, validate_reply, Lifecycle, Method, Status, BUS_NAME, MANAGE_ACTION,
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

struct Service {
    core: Arc<Mutex<Lifecycle>>,
    clients: Arc<Mutex<Value>>,
    worker: Option<Arc<Worker>>,
    demo_config: Mutex<Value>,
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
    async fn authorize(&self, connection: &Connection, header: &Header<'_>) -> fdo::Result<()> {
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
                        MANAGE_ACTION,
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

    async fn invoke(
        &self,
        method: Method,
        params: Value,
        connection: &Connection,
    ) -> fdo::Result<Value> {
        let permits = if method == Method::Stop {
            &self.stop_requests
        } else {
            &self.requests
        };
        let _permit = permits
            .try_acquire()
            .map_err(|_| fdo::Error::LimitsExceeded("Too many pending requests".into()))?;
        let epoch = self
            .core
            .lock()
            .unwrap()
            .begin(method)
            .map_err(|s| fdo::Error::Failed(s.into()))?;
        let result = match &self.worker {
            Some(worker) => worker.call(method, params).await,
            None => self.demo(method, params, connection).await,
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
                }
                if method == Method::Stop && success {
                    *self.clients.lock().unwrap() = json!([]);
                    let _ = connection
                        .emit_signal(
                            None::<&str>,
                            OBJECT_PATH,
                            BUS_NAME,
                            "ClientsChanged",
                            &("[]",),
                        )
                        .await;
                }
                if matches!(
                    method,
                    Method::Start | Method::Stop | Method::SwitchBandAndReconnect
                ) {
                    self.emit_status(connection).await;
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

    async fn demo(
        &self,
        method: Method,
        params: Value,
        connection: &Connection,
    ) -> Result<Value, String> {
        use std::sync::atomic::Ordering;
        match method {
            Method::Start => {
                let generation = self.demo_generation.load(Ordering::SeqCst);
                self.emit_status(connection).await;
                tokio::time::sleep(Duration::from_millis(400)).await;
                if generation != self.demo_generation.load(Ordering::SeqCst) {
                    return Ok(json!({"success":false,"error":"Startup cancelled"}));
                }
                let config = self.demo_config.lock().unwrap().clone();
                let status = Status {
                    active: true,
                    desired_active: true,
                    state: "active".into(),
                    ssid: config["SSID"].as_str().unwrap_or("RelayForge Demo").into(),
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
            Method::GetConfig => Ok(self.demo_config.lock().unwrap().clone()),
            Method::SetConfig => {
                let mut config = self.demo_config.lock().unwrap();
                for (key, value) in params.as_object().ok_or("Invalid config")? {
                    config[key] = value.clone();
                }
                Ok(json!(true))
            }
            Method::GetInterfaces => {
                Ok(json!({"wifi_interfaces":["demo0"],"all_interfaces":["demo0","demo1"]}))
            }
            Method::GetCapabilities => Ok(
                json!({"ap_2ghz":true,"ap_5ghz":false,"current_sta_band":"2.4","ap_sta_concurrent":false,"demo":true}),
            ),
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
        self.authorize(connection, &header).await?;
        Ok(self
            .invoke(Method::Start, Value::Null, connection)
            .await?
            .to_string())
    }
    async fn stop(
        &self,
        #[zbus(connection)] connection: &Connection,
        #[zbus(header)] header: Header<'_>,
    ) -> fdo::Result<bool> {
        self.authorize(connection, &header).await?;
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
        self.authorize(connection, &header).await?;
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
        self.authorize(connection, &header).await?;
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
        self.authorize(connection, &header).await?;
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
        self.authorize(connection, &header).await?;
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

fn verify_private_config() -> Result<(), Box<dyn std::error::Error>> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::symlink_metadata("/etc/wifi-hotspot.conf")?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.mode() & 0o777 != 0o600 {
        return Err(
            "Existing /etc/wifi-hotspot.conf must be a root-owned regular file with mode 0600"
                .into(),
        );
    }
    Ok(())
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

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (demo,deny,use_polkit,root)=match args.as_slice() {
        [mode] if mode=="--demo" => (true,false,false,None),
        [mode] if mode=="--demo-deny" => (true,true,false,None),
        [mode] if mode=="--demo-polkit" => (true,false,true,None),
        [mode,flag,path] if mode=="--system" && flag=="--legacy-root" => (false,false,true,Some(PathBuf::from(path))),
        _=>return Err("Usage: relayforge-service --demo | --demo-deny | --system --legacy-root /root-owned/installation".into()),
    };
    if !demo {
        // SAFETY: geteuid has no preconditions.
        if unsafe { libc::geteuid() } != 0 {
            return Err("System mode requires root".into());
        }
        verify_private_config()?;
        verify_tree(root.as_ref().unwrap())?;
    }
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
    let core = Arc::new(Mutex::new(Lifecycle::default()));
    let clients = Arc::new(Mutex::new(json!([])));
    let (events, mut receiver) = mpsc::channel::<backend::Event>(64);
    let worker = if let Some(root) = root {
        Some(Worker::spawn(&root, events).await?)
    } else {
        None
    };
    let service = Service {
        core: core.clone(),
        clients: clients.clone(),
        worker: worker.clone(),
        demo_config: Mutex::new(
            json!({"SSID":"RelayForge Demo","PASSPHRASE":"demo-only-password","BACKEND":"create_ap","WIFI_IFACE":"demo0","INTERNET_IFACE":"demo1"}),
        ),
        demo_deny: deny,
        use_polkit,
        demo_generation: std::sync::atomic::AtomicU64::new(0),
        requests: Semaphore::new(31),
        stop_requests: Semaphore::new(1),
    };
    connection.object_server().at(OBJECT_PATH, service).await?;
    connection
        .object_server()
        .remove::<Initializing, _>(OBJECT_PATH)
        .await?;
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
        } else {
            "compatibility backend"
        }
    );
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! { _=tokio::signal::ctrl_c()=>{}, _=terminate.recv()=>{} }
    shutdown_core.lock().unwrap().backend_lost();
    if let Some(worker) = worker {
        worker.shutdown().await?;
    }
    event_task.abort();
    Ok(())
}
