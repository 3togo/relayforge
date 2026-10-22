//! Non-networking child for real pidfd/recovery tests; never part of staged payloads.
fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert!(args.len() >= 2 && std::path::Path::new(&args[1]).is_file());
    if args.iter().any(|v| v == "--ignore-term") {
        unsafe {
            libc::signal(libc::SIGTERM, libc::SIG_IGN);
        }
    }
    std::fs::write(format!("{}.ready", args[1]), b"ready").unwrap();
    loop {
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
}
