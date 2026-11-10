//! Service construction, recovery, background tasks and orderly shutdown.
use super::*;
use crate::{create_ap, deployment, networkmanager, nm_journal};
use std::path::{Path, PathBuf};
use tokio::sync::mpsc;

/// Exit status for "never configured". Packaged units prevent restart for it.
const EXIT_MISSING_CONFIG: i32 = 17;

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

pub(crate) async fn run(
    args: Vec<String>,
    allow_existing_bridge: bool,
    allow_band_switch: bool,
) -> Result<(), Box<dyn std::error::Error>> {
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
