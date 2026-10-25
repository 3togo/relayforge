//! Release orphaned hostapd processes whose owned instance directory is already gone.
//!
//! A journal only records resources this service believes it owns. A hard crash can
//! leave a hostapd process running against a configuration directory that cleanup has
//! since removed. Those processes are still identifiable: their command line points at
//! `hostapd.conf` inside this service's private runtime, under an instance directory
//! that no longer exists. Nothing else is ever signalled.
use crate::{
    ap_process::{Birth, Handle},
    nm_journal::Result,
};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
const PROC: &str = "/proc";
const HOSTAPD: &str = "hostapd";
const CONF: &str = "hostapd.conf";
const TERM_TIMEOUT: Duration = Duration::from_secs(3);

fn name(path: &Path) -> Option<String> {
    path.file_name().map(|v| v.to_string_lossy().into_owned())
}

fn arguments(pid: u32) -> Option<Vec<String>> {
    let mut bytes = Vec::new();
    fs::File::open(Path::new(PROC).join(pid.to_string()).join("cmdline"))
        .and_then(|file| {
            use std::io::Read;
            file.take(16385).read_to_end(&mut bytes)
        })
        .ok()?;
    if bytes.len() > 16384 {
        return None;
    }
    let text = String::from_utf8(bytes).ok()?;
    Some(
        text.trim_end_matches('\0')
            .split('\0')
            .map(str::to_owned)
            .collect(),
    )
}

/// The configuration directory of an instance, `create_ap.<iface>.conf.<random>`.
fn instance_directory(directory: &Path) -> bool {
    let Some(name) = name(directory) else {
        return false;
    };
    let Some((prefix, tail)) = name.rsplit_once(".conf.") else {
        return false;
    };
    if tail.len() != 8 || !tail.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return false;
    }
    prefix.starts_with("create_ap.")
        && prefix.len() > "create_ap.".len()
        && prefix["create_ap.".len()..]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
}

/// A hostapd process is stale only when it runs our configuration and its instance
/// directory has already been removed by cleanup.
fn stale(root: &Path, pid: u32) -> Option<(String, Vec<String>)> {
    if pid <= 1 {
        return None;
    }
    let process = PathBuf::from(PROC).join(pid.to_string());
    if fs::metadata(&process).ok()?.uid() != unsafe { libc::geteuid() } {
        return None;
    }
    let exe = fs::read_link(process.join("exe")).ok()?;
    let exe = exe
        .to_string_lossy()
        .trim_end_matches(" (deleted)")
        .to_owned();
    if name(Path::new(&exe)).as_deref() != Some(HOSTAPD) {
        return None;
    }
    let args = arguments(pid)?;
    if args.is_empty() || name(Path::new(&args[0])).as_deref() != Some(HOSTAPD) {
        return None;
    }
    let config = Path::new(args.last()?);
    if name(config).as_deref() != Some(CONF) {
        return None;
    }
    let directory = config.parent()?;
    if directory.parent() != Some(root) || !instance_directory(directory) || directory.exists() {
        return None;
    }
    Some((exe, args))
}

/// Every orphaned hostapd process inside `root`, with the identity it must keep to be
/// signalled. Re-checked immediately before signalling, so a reused PID is never hit.
pub fn orphans(root: &Path) -> Vec<(u32, String, Vec<String>)> {
    let mut result = Vec::new();
    let entries = match fs::read_dir(PROC) {
        Ok(entries) => entries,
        Err(_) => return result,
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let pid = match name(&entry.path()).and_then(|v| v.parse::<u32>().ok()) {
            Some(pid) => pid,
            None => continue,
        };
        if let Some((exe, args)) = stale(root, pid) {
            result.push((pid, exe, args));
        }
    }
    result
}

/// Terminate orphaned hostapd processes, waiting for each to exit. Refuses to report
/// success while one of them is still running.
pub async fn reap(root: &Path) -> Result<usize> {
    let mut released = 0;
    for (pid, exe, args) in orphans(root) {
        let birth = match Birth::capture(pid) {
            Ok(birth) => birth,
            Err(_) => continue,
        };
        let handle = match Handle::open(&birth) {
            Ok(Some(handle)) => handle,
            _ => continue,
        };
        // The process may have been replaced between discovery and pidfd_open.
        if stale(root, pid).as_ref() != Some(&(exe.clone(), args.clone())) {
            continue;
        }
        handle.signal(libc::SIGTERM, &[(exe, args)])?;
        let deadline = Instant::now() + TERM_TIMEOUT;
        while !handle.exited()? && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        if !handle.exited()? {
            return Err("A stale Relay hostapd process did not exit");
        }
        released += 1;
    }
    Ok(released)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn instance_directory_accepts_only_the_owned_layout() {
        assert!(instance_directory(Path::new("create_ap.wlp2s0.conf.Ab123456")));
        assert!(instance_directory(Path::new("create_ap.wlan0:1.conf.00000000")));
        for directory in [
            "create_ap.wlp2s0.conf.Ab12345",
            "create_ap.wlp2s0.conf.Ab1234567",
            "create_ap..conf.Ab123456",
            "create_ap.wlp2s0.conf.Ab-23456",
            "foreign.wlp2s0.conf.Ab123456",
            "create_ap.wlp2s0",
        ] {
            assert!(!instance_directory(Path::new(directory)), "{directory}");
        }
    }
    #[tokio::test]
    async fn a_foreign_runtime_never_yields_orphans() {
        let path = std::env::temp_dir().join(format!(
            "relayforge-stale-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(path.join("create-ap")).unwrap();
        assert!(orphans(&path.join("create-ap")).is_empty());
        assert_eq!(reap(&path.join("create-ap")).await.unwrap(), 0);
        std::fs::remove_dir_all(path).unwrap();
    }
}
