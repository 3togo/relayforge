//! Standalone commands. These finish before service construction or bus ownership.
use crate::{ap_runtime, config_store, create_ap, deployment, inventory, radio};
use relayforge_core::config::{self, Config};
use serde_json::json;
use std::path::Path;

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
                eprintln!("No unambiguous Wi-Fi adapter detected; keeping the default interface");
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

/// Return true when a standalone command completed; false selects service startup.
pub(crate) async fn run(args: &[String]) -> Result<bool, Box<dyn std::error::Error>> {
    if args == ["--deployment-protocol"] {
        println!("{}", deployment::live::PROTOCOL);
        return Ok(true);
    }
    if let Some(mode @ ("--deploy-live" | "--test-deploy-live")) = args.first().map(String::as_str)
    {
        deployment::live::run(&args[1..], mode == "--test-deploy-live").await?;
        return Ok(true);
    }
    if let Some(mode @ ("--deploy" | "--test-deploy")) = args.first().map(String::as_str) {
        deployment::run(&args[1..], mode == "--test-deploy")?;
        return Ok(true);
    }
    if let Some(mode @ ("--create-ap-instance" | "--test-ap-instance")) =
        args.first().map(String::as_str)
    {
        ap_runtime::run_instance(&args[1..], mode == "--test-ap-instance").await?;
        return Ok(true);
    }
    if args.first().map(String::as_str) == Some("--create-ap-exec") {
        create_ap::exec_gate(args)?;
        return Ok(true);
    }
    if let Some(mode) = args.first().map(String::as_str) {
        if matches!(mode, "--create-ap-runtime" | "--test-ap-runtime") {
            match ap_runtime::run(&args[1..], mode == "--test-ap-runtime").await {
                Ok(output) => {
                    if !output.is_empty() {
                        println!("{output}");
                    }
                    return Ok(true);
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
    // Diagnostic modes return before bus registration or worker construction.
    // They run read-only iw inspection and never apply a startup plan.
    if let [mode, target] = args {
        if mode == "--inspect-radio" {
            let snapshot = radio::Source::System.inspect(target).await?;
            println!("{}", serde_json::to_string(&snapshot.capabilities)?);
            return Ok(true);
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
            return Ok(true);
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
        return Ok(true);
    }
    if let Some(mode @ ("--init-config" | "--test-init-config")) = args.first().map(String::as_str)
    {
        // The test variant selects the interface inventory root instead of sysfs.
        let (net, arguments) = if mode == "--test-init-config" {
            (
                Path::new(
                    args.get(1)
                        .ok_or("--test-init-config requires a net root")?,
                ),
                &args[2..],
            )
        } else {
            (Path::new("/sys/class/net"), &args[1..])
        };
        init_config(arguments, net)?;
        return Ok(true);
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
        repair_config_interface(arguments, net)?;
        return Ok(true);
    }
    Ok(false)
}
