mod ap_firewall;
mod ap_process;
mod ap_runtime;
mod backend;
mod band_switch;
mod cli;
mod config_store;
mod create_ap;
mod deployment;
mod inventory;
mod native_backend;
mod networkmanager;
mod nm_journal;
mod radio;
mod service;
mod stale;

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
    if cli::run(&args).await? {
        return Ok(());
    }
    service::run(args, allow_existing_bridge, allow_band_switch).await
}
