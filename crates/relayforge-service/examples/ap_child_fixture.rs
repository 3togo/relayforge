//! Non-networking child for real pidfd/recovery tests; never staged.
fn main() {
    use std::{
        path::Path,
        time::{Duration, Instant},
    };
    let args: Vec<String> = std::env::args().collect();
    let name = Path::new(&args[0]).file_name().unwrap().to_str().unwrap();
    let config = args
        .iter()
        .find_map(|v| v.strip_prefix("--conf-file="))
        .unwrap_or_else(|| args.get(1).expect("Missing fixture config"));
    assert!(Path::new(config).is_file());
    let controls = Path::new(config)
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("daemon-controls.json");
    let fixture_directory = Path::new(config)
        .parent()
        .unwrap()
        .file_name()
        .unwrap()
        .to_string_lossy()
        .starts_with("create_ap.");
    let options: serde_json::Value = fixture_directory
        .then(|| std::fs::read(controls).ok())
        .flatten()
        .map(|v| serde_json::from_slice(&v).unwrap())
        .unwrap_or_else(|| serde_json::json!({}));
    if options["fail_child"].as_str() == Some(name) {
        std::process::exit(3);
    }
    if args.iter().any(|v| v == "--ignore-term") || options["ignore_term"].as_bool() == Some(true) {
        unsafe {
            libc::signal(libc::SIGTERM, libc::SIG_IGN);
        }
    }
    if let Some(path) = args
        .iter()
        .find_map(|v| v.strip_prefix("--dhcp-leasefile="))
    {
        std::fs::write(path, b"0 02:aa:bb:cc:dd:ee 192.168.12.2 Rust-client *\n").unwrap();
    }
    std::fs::write(format!("{config}.ready"), b"ready").unwrap();
    if options["flood_child"].as_str() == Some(name) {
        use std::io::Write;
        let bytes = vec![b'X'; 16384];
        for _ in 0..80 {
            if std::io::stdout().write_all(&bytes).is_err() {
                break;
            }
        }
    }
    if options["close_output"].as_bool() == Some(true) {
        unsafe {
            libc::close(1);
            libc::close(2);
        }
    }
    let started = Instant::now();
    loop {
        if options["exit_child"].as_str() == Some(name)
            && started.elapsed().as_millis() > options["exit_ms"].as_u64().unwrap_or(200) as u128
        {
            std::process::exit(4);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
