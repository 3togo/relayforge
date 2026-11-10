//! Native create_ap supervision, with gated Rust instance processes.
use crate::{
    ap_process::{self, Birth, Handle},
    config_store::random_secret,
    nm_journal::{Journal, Record, Result},
    radio,
};
use relayforge_core::{
    config::{self, Config},
    radio::{Capture, Snapshot},
    Status,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    ffi::CString,
    fs::{self, OpenOptions},
    io::Read,
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
    sync::{watch, Mutex},
    task::JoinHandle,
    time::{sleep, timeout, Instant},
};
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Owned {
    version: u32,
    token: String,
    parent: String,
    helper: String,
    runner: String,
    band: String,
    channel: u16,
    process: Option<Birth>,
    #[serde(default)]
    bridge: bool,
}
impl Record for Owned {
    fn validate_record(&self) -> Result<()> {
        if self.version != 1
            || self.token.len() != 32
            || !self.token.bytes().all(|b| b.is_ascii_hexdigit())
            || !["2.4", "5"].contains(&self.band.as_str())
            || !(1..=196).contains(&self.channel)
            || !Path::new(&self.helper).is_absolute()
            || !Path::new(&self.runner).is_absolute()
        {
            return Err("Invalid create_ap ownership record");
        }
        config::parse_patch(&json!({"WIFI_IFACE":self.parent}).to_string())?;
        if let Some(process) = &self.process {
            process.validate()?;
        }
        Ok(())
    }
}
impl Owned {
    fn file(&self, suffix: &str) -> CString {
        CString::new(format!("create-ap-{}.{}", self.token, suffix)).unwrap()
    }
    fn config(&self, root: &Path) -> PathBuf {
        root.join(self.file("conf").to_str().unwrap())
    }
    fn pidfile(&self, root: &Path) -> PathBuf {
        root.join(self.file("pid").to_str().unwrap())
    }
    fn args(&self, root: &Path) -> Vec<String> {
        let mut args = vec![
            self.helper.clone(),
            "--config".into(),
            self.config(root).to_string_lossy().into(),
            "--pidfile".into(),
            self.pidfile(root).to_string_lossy().into(),
            "--freq-band".into(),
            self.band.clone(),
            "-c".into(),
            self.channel.to_string(),
            "--daemon".into(),
            "--no-haveged".into(),
        ];
        if self.bridge {
            args.push("--allow-existing-bridge".into());
        }
        args
    }
    fn gate_args(&self, root: &Path) -> Vec<String> {
        let mut args = vec![
            self.runner.clone(),
            "--create-ap-exec".into(),
            self.helper.clone(),
            self.config(root).to_string_lossy().into(),
            self.pidfile(root).to_string_lossy().into(),
            self.band.clone(),
            self.channel.to_string(),
        ];
        if self.bridge {
            args.push("--allow-existing-bridge".into());
        }
        args
    }
    fn allowed(&self, root: &Path, fixture: bool) -> Vec<(String, Vec<String>)> {
        let bash = fs::canonicalize("/bin/bash")
            .unwrap_or_else(|_| PathBuf::from("/usr/bin/bash"))
            .to_string_lossy()
            .into_owned();
        let mut args = vec![bash.clone()];
        args.extend(self.args(root));
        let mut native = vec![self.helper.clone(), "--create-ap-instance".into()];
        native.extend(self.args(root).into_iter().skip(1));
        let mut allowed = vec![
            (self.runner.clone(), self.gate_args(root)),
            (bash, args),
            (self.helper.clone(), native),
        ];
        if fixture {
            let mut test_args = vec![
                root.join("runtime-bin").to_string_lossy().into_owned(),
                "--test-ap-instance".into(),
                root.to_string_lossy().into_owned(),
            ];
            test_args.extend(self.args(root).into_iter().skip(1));
            allowed.push((self.runner.clone(), test_args));
        }
        allowed
    }
}
/// The child cannot exec the network helper until the parent durably records its birth.
pub fn exec_gate(args: &[String]) -> std::result::Result<(), Box<dyn std::error::Error>> {
    if !(args.len() == 6 || args.len() == 7 && args[6] == "--allow-existing-bridge") {
        return Err("Invalid create_ap execution gate".into());
    }
    let mut ack = [0u8; 1];
    std::io::stdin().read_exact(&mut ack)?;
    if ack != [1] {
        return Err("Invalid create_ap execution acknowledgement".into());
    }
    let owned = Owned {
        version: 1,
        token: "0".repeat(32),
        parent: "gate0".into(),
        helper: ap_process::trusted_file(Path::new(&args[1]))?,
        runner: String::new(),
        band: args[4].clone(),
        channel: args[5].parse()?,
        process: None,
        bridge: args.len() == 7,
    };
    if !["2.4", "5"].contains(&owned.band.as_str()) || !(1..=196).contains(&owned.channel) {
        return Err("Invalid create_ap radio plan".into());
    }
    unsafe {
        libc::umask(0o077);
    }
    let mut command = if elf(Path::new(&owned.helper))? {
        let mut command = std::process::Command::new(&owned.helper);
        command.arg("--create-ap-instance");
        command
    } else {
        let mut command = std::process::Command::new(fs::canonicalize("/bin/bash")?);
        command.arg(&owned.helper);
        command
    };
    if unsafe { libc::geteuid() } == 0 && !elf(Path::new(&owned.helper))? {
        let bytes = private_read(Path::new(&args[2]), 65536, 0o600)?
            .ok_or("Missing frozen helper configuration")?;
        let config = config::parse_file(
            std::str::from_utf8(&bytes).map_err(|_| "Invalid frozen helper configuration")?,
        )?;
        helper_policy(&config, false, false)?;
        if owned.bridge {
            return Err("Existing-bridge mode requires the native ELF helper".into());
        }
    }
    command.args([
        "--config",
        &args[2],
        "--pidfile",
        &args[3],
        "--freq-band",
        &owned.band,
        "-c",
        &args[5],
        "--daemon",
        "--no-haveged",
    ]);
    if owned.bridge {
        command.arg("--allow-existing-bridge");
    }
    let error = command.stdin(Stdio::null()).exec();
    Err(error.into())
}
#[derive(Clone)]
pub struct Platform {
    pub fixture: bool,
    pub runtime: PathBuf,
}
impl Platform {
    fn engine(&self) -> PathBuf {
        if self.fixture {
            self.runtime.join("engine")
        } else {
            PathBuf::from("/run/wifi-relay/create-ap")
        }
    }
    async fn radio(&self, iface: &str) -> Result<Snapshot> {
        if self.fixture {
            let bytes = private_read(&self.runtime.join("live-radio.json"), 65536, 0o600)?
                .ok_or("Missing simulated radio")?;
            let capture: Capture =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid simulated radio")?;
            if capture.wifi_iface != iface {
                return Err("Simulated adapter mismatch");
            }
            Snapshot::parse(&capture)
        } else {
            radio::Source::System.inspect(iface).await
        }
    }
    async fn observe(&self, owned: &Owned, directory: &Path) -> Result<(String, u32, Value)> {
        if self.fixture {
            let text = run_helper(
                &owned.helper,
                &[
                    "--rf-observe",
                    owned
                        .pidfile(&self.runtime)
                        .to_str()
                        .ok_or("Invalid runtime path")?,
                ],
                self,
                3,
            )
            .await?;
            let report: Value =
                serde_json::from_str(&text).map_err(|_| "Invalid simulated AP observation")?;
            let iface = report["interface"]
                .as_str()
                .ok_or("Missing AP interface")?
                .to_owned();
            let frequency = report["frequency"]
                .as_u64()
                .filter(|f| *f <= 100000)
                .ok_or("Invalid AP frequency")? as u32;
            let clients = report["clients"].clone();
            return Ok((iface, frequency, clients));
        }
        let snapshot = recovery(directory)?;
        let iface = snapshot
            .fields
            .get("VWIFI_IFACE")
            .filter(|v| !v.is_empty())
            .or_else(|| snapshot.fields.get("WIFI_IFACE"))
            .ok_or("Recovery snapshot lacks AP interface")?
            .clone();
        config::parse_patch(&json!({"WIFI_IFACE":iface}).to_string())?;
        let iw = ["/usr/sbin/iw", "/usr/bin/iw", "/sbin/iw", "/bin/iw"]
            .into_iter()
            .map(Path::new)
            .find(|p| p.is_file())
            .ok_or("iw is unavailable")?;
        let text = radio::command(iw, &["dev", &iface, "info"], Duration::from_secs(3)).await?;
        let (_, kind) = relayforge_core::radio::device(&text, &iface)?;
        if kind != "AP" {
            return Err("Owned interface is not an AP");
        }
        let frequency = text
            .lines()
            .map(str::trim)
            .filter(|l| l.starts_with("channel "))
            .find_map(|l| {
                l.split_once('(')
                    .and_then(|(_, r)| r.split_once(" MHz)"))
                    .and_then(|(v, _)| v.parse::<f64>().ok())
            })
            .filter(|f| f.is_finite() && f.fract() == 0.0 && (1.0..=100000.0).contains(f))
            .ok_or("Cannot verify AP channel")? as u32;
        let net = PathBuf::from("/sys/class/net").join(&iface);
        let expected = snapshot
            .interfaces
            .get(&iface)
            .ok_or("AP identity was not recorded")?;
        let index =
            fs::read_to_string(net.join("ifindex")).map_err(|_| "AP interface disappeared")?;
        let mac = fs::read_to_string(net.join("address")).map_err(|_| "Cannot inspect AP MAC")?;
        if index.trim().parse::<u32>().ok() != Some(expected.ifindex)
            || !mac.trim().eq_ignore_ascii_case(&expected.mac)
        {
            return Err("AP interface identity changed");
        }
        let stations = radio::command(
            iw,
            &["dev", &iface, "station", "dump"],
            Duration::from_secs(3),
        )
        .await?;
        let leases = crate::ap_runtime::daemon_leases(directory, None)?;
        let leases = String::from_utf8(leases).map_err(|_| "Invalid DHCP lease data")?;
        let mut addresses = BTreeMap::new();
        for line in leases.lines() {
            let fields: Vec<_> = line.split_whitespace().collect();
            if fields.len() >= 4 && fields[2].parse::<std::net::Ipv4Addr>().is_ok() {
                addresses.insert(
                    fields[1].to_lowercase(),
                    (
                        fields[2].to_owned(),
                        if fields[3] == "*" {
                            String::new()
                        } else {
                            fields[3].to_owned()
                        },
                    ),
                );
            }
        }
        // A station without a DHCP lease (static addressing) still appears in the
        // kernel neighbor table; the pinned daemon falls back to it for the IP and
        // never claims a hostname that the lease file did not provide.
        if let Some(arp) = arp_neighbors() {
            for (mac, ip) in arp_addresses(&arp) {
                addresses.entry(mac).or_insert((ip, String::new()));
            }
        }
        let mut clients = Vec::new();
        for mac in stations
            .lines()
            .filter_map(|l| l.strip_prefix("Station "))
            .filter_map(|r| r.split_whitespace().next())
        {
            if mac.len() != 17
                || !mac
                    .split(':')
                    .all(|v| v.len() == 2 && v.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err("Invalid station identity");
            }
            let mac = mac.to_lowercase();
            let (ip, hostname) = addresses.get(&mac).cloned().unwrap_or_default();
            clients.push(json!({"mac":mac,"ip":ip,"hostname":hostname}));
        }
        Ok((iface, frequency, json!(clients)))
    }
}
/// Bounded `/proc/net/arp` read; an unreadable table is not a client failure.
fn arp_neighbors() -> Option<String> {
    use std::io::Read;
    let mut bytes = Vec::new();
    std::fs::File::open("/proc/net/arp")
        .ok()?
        .take(64 * 1024)
        .read_to_end(&mut bytes)
        .ok()?;
    String::from_utf8(bytes).ok()
}
/// MAC→IPv4 map from the kernel neighbor table. Incomplete placeholders
/// (`00:00:00:00:00:00`) and malformed rows are never addresses for a client.
fn arp_addresses(text: &str) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    for line in text.lines().skip(1) {
        let fields: Vec<&str> = line.split_whitespace().collect();
        // <ip> <hw-type> <flags> <mac> <mask> <device>
        if fields.len() < 6 {
            continue;
        }
        let mac = fields[3].to_lowercase();
        if mac == "00:00:00:00:00:00"
            || mac.len() != 17
            || !mac
                .split(':')
                .all(|part| part.len() == 2 && part.bytes().all(|b| b.is_ascii_hexdigit()))
        {
            continue;
        }
        if fields[0].parse::<std::net::Ipv4Addr>().is_ok() {
            map.insert(mac, fields[0].to_owned());
        }
    }
    map
}
fn private_read(path: &Path, limit: usize, mode: u32) -> Result<Option<Vec<u8>>> {
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
    {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("Cannot open helper recovery file"),
    };
    let meta = file
        .metadata()
        .map_err(|_| "Cannot inspect helper recovery file")?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
        || (if mode == 0 {
            meta.mode() & 0o022 != 0
        } else {
            meta.mode() & 0o7777 != mode
        })
        || meta.len() > limit as u64
    {
        return Err("Unsafe helper recovery file");
    }
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Cannot read helper recovery file")?;
    if bytes.len() > limit {
        return Err("Oversized helper recovery file");
    }
    Ok(Some(bytes))
}
#[derive(Deserialize)]
struct RecoveryOwner {
    pid: u32,
    starttime: String,
}
#[derive(Deserialize)]
struct InterfaceIdentity {
    ifindex: u32,
    mac: String,
}
#[derive(Deserialize)]
struct Recovery {
    owner: RecoveryOwner,
    fields: BTreeMap<String, String>,
    interfaces: BTreeMap<String, InterfaceIdentity>,
}
fn recovery(path: &Path) -> Result<Recovery> {
    let bytes = private_read(&path.join("recovery.json"), 65536, 0o600)?
        .ok_or("Missing helper recovery snapshot")?;
    serde_json::from_slice(&bytes).map_err(|_| "Invalid helper recovery snapshot")
}
fn private_directory(path: &Path, exact: bool) -> Result<()> {
    let meta = fs::symlink_metadata(path).map_err(|_| "Cannot inspect helper runtime directory")?;
    if !meta.is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || if exact {
            meta.mode() & 0o7777 != 0o700
        } else {
            meta.mode() & 0o022 != 0
        }
    {
        return Err("Unsafe helper runtime directory");
    }
    Ok(())
}
fn elf(path: &Path) -> Result<bool> {
    let mut magic = [0u8; 4];
    std::fs::File::open(path)
        .and_then(|mut file| file.read_exact(&mut magic))
        .map_err(|_| "Cannot inspect helper executable format")?;
    Ok(&magic == b"\x7fELF")
}
fn helper_policy(config: &Config, native: bool, fixture: bool) -> Result<()> {
    if !native && !fixture {
        if config.contains_key("COUNTRY") {
            return Err("Read-only COUNTRY policy requires the native ELF helper; update the native installation before starting");
        }
        if config["SHARE_METHOD"] == "bridge" {
            return Err("Existing-bridge mode requires the native ELF helper");
        }
    }
    Ok(())
}
#[cfg(test)]
mod helper_policy_tests {
    use super::*;
    #[test]
    fn production_country_and_bridge_constraints_cannot_use_a_legacy_helper() {
        let mut config = config::defaults();
        assert!(helper_policy(&config, false, false).is_ok());
        config.insert("COUNTRY".into(), "US".into());
        let before = config.clone();
        assert!(helper_policy(&config, false, false)
            .unwrap_err()
            .contains("native ELF"));
        assert!(helper_policy(&config, true, false).is_ok());
        assert!(helper_policy(&config, false, true).is_ok());
        assert_eq!(config, before);
        config.remove("COUNTRY");
        config.insert("SHARE_METHOD".into(), "bridge".into());
        assert!(helper_policy(&config, false, false).is_err());
        assert!(helper_policy(&config, true, false).is_ok());
    }
}
async fn run_helper(
    program: &str,
    args: &[&str],
    platform: &Platform,
    seconds: u64,
) -> Result<String> {
    let native = elf(Path::new(program))?;
    let mut arguments = if native {
        vec!["--create-ap-instance"]
    } else {
        vec![program]
    };
    arguments.extend_from_slice(args);
    let mut environment = std::collections::BTreeMap::from([
        (
            std::ffi::OsString::from("PATH"),
            std::ffi::OsString::from("/usr/sbin:/usr/bin:/sbin:/bin"),
        ),
        (
            std::ffi::OsString::from("LC_ALL"),
            std::ffi::OsString::from("C"),
        ),
    ]);
    if platform.fixture {
        environment.insert(
            "RELAYFORGE_TEST_STATE".into(),
            platform.runtime.as_os_str().to_owned(),
        );
    }
    radio::command_env(
        if native {
            Path::new(program)
        } else {
            Path::new("/bin/bash")
        },
        &arguments,
        Duration::from_secs(seconds),
        Some(&environment),
    )
    .await
}
/// Bounded tail of helper output, retained so a failed startup can explain itself.
type Tail = Arc<std::sync::Mutex<Vec<u8>>>;
const TAIL_LIMIT: usize = 16 * 1024;
const TAIL_LINES: usize = 4;
async fn capture(mut pipe: impl AsyncRead + Unpin, tail: Tail) -> Result<()> {
    let mut buffer = [0u8; 8192];
    let mut total = 0usize;
    loop {
        let count = pipe
            .read(&mut buffer)
            .await
            .map_err(|_| "Cannot read helper output")?;
        if count == 0 {
            return Ok(());
        }
        total += count;
        let mut guard = tail.lock().unwrap_or_else(|error| error.into_inner());
        guard.extend_from_slice(&buffer[..count]);
        if guard.len() > TAIL_LIMIT {
            let excess = guard.len() - TAIL_LIMIT;
            guard.drain(..excess);
        }
        // The tail is bounded, but a helper that never stops writing is still a fault.
        if total > 1024 * 1024 {
            return Err("create_ap output exceeds limit");
        }
    }
}
/// Credentials never reach a D-Bus reply, so any stored secret is masked first.
fn redact(text: &str, secret: &str) -> String {
    if secret.len() < 4 {
        return text.to_owned();
    }
    let mut result = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(index) = rest.find(secret) {
        result.push_str(&rest[..index]);
        result.push_str("[redacted]");
        rest = &rest[index + secret.len()..];
    }
    result.push_str(rest);
    result
}
/// Publish the helper's own explanation to the service journal, never to a reply.
///
/// Helper output is untrusted: create_ap and hostapd logs can echo configuration.
/// Credentials never travel over the bus, so the tail is redacted and written to
/// stderr, where an operator can read it without exposing it to every desktop client.
pub(crate) fn report_startup_log(tail: &Tail, config: &Config) -> bool {
    let Ok(bytes) = tail.lock() else {
        return false;
    };
    let secret = config.get("PASSPHRASE").map(String::as_str).unwrap_or("");
    let text = redact(&String::from_utf8_lossy(&bytes), secret);
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    if lines.is_empty() {
        return false;
    }
    let start = lines.len().saturating_sub(TAIL_LINES);
    eprintln!(
        "create_ap startup diagnostics: {}",
        lines[start..].join(" \n")
    );
    true
}
struct Session {
    owned: Owned,
    child: Child,
    output: Vec<JoinHandle<Result<()>>>,
    log: Tail,
    status: Status,
    baseline: (String, String, u32),
    frequency: u32,
}
pub struct Backend {
    platform: Platform,
    helper: String,
    runner: String,
    journal: Mutex<Journal<Owned>>,
    session: Mutex<Option<Session>>,
    allow_bridge: bool,
}
impl Backend {
    pub async fn environment_preflight(&self, config: &Config, phy: u32) -> Result<()> {
        helper_policy(config, elf(Path::new(&self.helper))?, self.platform.fixture)?;
        crate::ap_runtime::environment_preflight(
            config,
            phy,
            self.platform
                .fixture
                .then_some(self.platform.runtime.as_path()),
        )
        .await
    }
    pub fn driver_preflight(&self, config: &Config) -> Result<String> {
        crate::ap_runtime::driver_preflight(
            config,
            self.platform
                .fixture
                .then_some(self.platform.runtime.as_path()),
        )
    }
    pub async fn bridge_preflight(&self, config: &Config) -> Result<()> {
        crate::ap_runtime::existing_bridge_preflight(
            config,
            self.platform
                .fixture
                .then_some(self.platform.runtime.as_path()),
        )
        .await
    }
    pub fn new(platform: Platform, program: &Path, allow_bridge: bool) -> Result<Arc<Self>> {
        private_directory(&platform.runtime, true)?;
        let helper = ap_process::trusted_file(program)?;
        let runner = std::env::current_exe()
            .map_err(|_| "Cannot locate service executable")?
            .canonicalize()
            .map_err(|_| "Cannot locate service executable")?
            .to_string_lossy()
            .into_owned();
        let journal = Journal::open_named(
            &platform.runtime,
            unsafe { libc::geteuid() },
            c"create-ap.json",
        )?;
        Ok(Arc::new(Self {
            platform,
            helper,
            runner,
            journal: Mutex::new(journal),
            session: Mutex::new(None),
            allow_bridge,
        }))
    }
    fn directories(&self, owned: &Owned) -> Result<Vec<PathBuf>> {
        let root = self.platform.engine();
        if !root.exists() {
            return Ok(Vec::new());
        }
        private_directory(&root, true)?;
        if !self.platform.fixture {
            private_directory(root.parent().unwrap(), true)?;
        }
        let marker = owned
            .pidfile(&self.platform.runtime)
            .to_string_lossy()
            .into_owned();
        let mut result = Vec::new();
        for entry in
            fs::read_dir(&root).map_err(|_| "Cannot inspect helper recovery directories")?
        {
            let path = entry
                .map_err(|_| "Cannot inspect helper recovery directory")?
                .path();
            let name = path.file_name().unwrap().to_string_lossy();
            let prefix = format!("create_ap.{}.conf.", owned.parent);
            if !name.starts_with(&prefix) {
                continue;
            }
            let tail = &name[prefix.len()..];
            if tail.len() != 8 || !tail.bytes().all(|b| b.is_ascii_alphanumeric()) {
                return Err("Invalid helper recovery directory name");
            }
            private_directory(&path, false)?;
            if !path.join("recovery.json").exists() {
                // The helper removes its pid file before checking cleanup. An
                // absent pid therefore cannot prove that this directory is
                // unrelated or clean when its recovery snapshot is also lost.
                return Err("Helper state has no recovery snapshot; ownership is unconfirmed");
            }
            let data = recovery(&path)?;
            if data.fields.get("DAEMON_PIDFILE") != Some(&marker) {
                continue;
            }
            let process = owned
                .process
                .as_ref()
                .ok_or("Resource snapshot has no recorded process")?;
            if data.owner.pid != process.pid
                || data.owner.starttime != process.start
                || data.fields.get("CONFDIR") != path.to_str().map(str::to_owned).as_ref()
            {
                return Err("Helper recovery ownership changed");
            }
            result.push(path);
        }
        Ok(result)
    }
    async fn stop_process(&self, owned: &Owned, mut child: Option<&mut Child>) -> Result<()> {
        if owned.helper != self.helper {
            return Err("Recorded helper installation differs; refusing recovery");
        }
        if let Some(birth) = &owned.process {
            if let Some(handle) = Handle::open(birth)? {
                let allowed = owned.allowed(&self.platform.runtime, self.platform.fixture);
                handle.signal(libc::SIGUSR1, &allowed)?;
                let deadline = Instant::now()
                    + Duration::from_millis(if self.platform.fixture { 350 } else { 30000 });
                while !handle.exited()? && Instant::now() < deadline {
                    sleep(Duration::from_millis(25)).await;
                }
                if !handle.exited()? {
                    handle.signal(libc::SIGTERM, &allowed)?;
                    sleep(Duration::from_millis(if self.platform.fixture {
                        100
                    } else {
                        1000
                    }))
                    .await;
                }
                if !handle.exited()? {
                    handle.signal(libc::SIGKILL, &allowed)?;
                }
                let deadline = Instant::now() + Duration::from_secs(2);
                while !handle.exited()? && Instant::now() < deadline {
                    sleep(Duration::from_millis(25)).await;
                }
                if !handle.exited()? {
                    return Err("Owned create_ap process did not exit");
                }
            }
            if let Some(child) = child.as_mut() {
                // A direct child is retained unreaped until this group signal,
                // so its process-group ID cannot be reused. Recovery never does this.
                if child.id() == Some(birth.pid) {
                    unsafe {
                        libc::kill(-(birth.pid as i32), libc::SIGKILL);
                    }
                }
                timeout(Duration::from_secs(2), child.wait())
                    .await
                    .map_err(|_| "Cannot reap owned helper")?
                    .map_err(|_| "Cannot reap owned helper")?;
            }
        }
        Ok(())
    }
    /// Release hostapd processes whose instance directory cleanup already removed.
    async fn reap_orphans(&self) -> Result<()> {
        if self.platform.fixture {
            return Ok(());
        }
        crate::stale::reap(&self.platform.engine()).await?;
        Ok(())
    }
    async fn recover_owned(&self, owned: &Owned) -> Result<()> {
        crate::ap_runtime::finish_instance_cleanup(
            &owned.pidfile(&self.platform.runtime),
            self.platform
                .fixture
                .then_some(self.platform.runtime.as_path()),
        )
        .await?;
        for directory in self.directories(owned)? {
            run_helper(
                &self.helper,
                &[
                    "--recover",
                    directory
                        .to_str()
                        .ok_or("Invalid recovery directory path")?,
                ],
                &self.platform,
                40,
            )
            .await?;
            if directory.exists() {
                return Err("create_ap resource cleanup is unconfirmed");
            }
        }
        if !self.directories(owned)?.is_empty() {
            return Err("Owned recovery resources remain");
        }
        let mut journal = self.journal.lock().await;
        journal.load()?;
        for suffix in ["conf", "pid", "pid.ready"] {
            journal.remove_private(&owned.file(suffix))?;
        }
        journal.clear()
    }
    pub async fn recover(&self) -> Result<()> {
        self.reap_orphans().await?;
        let owned = self.journal.lock().await.load()?;
        if let Some(owned) = owned {
            self.stop_process(&owned, None).await?;
            self.recover_owned(&owned).await?;
        }
        Ok(())
    }
    async fn cleanup(&self, session: &mut Session) -> Result<()> {
        self.journal.lock().await.load()?;
        self.stop_process(&session.owned, Some(&mut session.child))
            .await?;
        for task in &session.output {
            task.abort();
        }
        self.recover_owned(&session.owned).await
    }
    async fn verify(&self, session: &mut Session) -> Result<Value> {
        if session.output.iter().any(|t| t.is_finished()) {
            return Err("create_ap exited or its output exceeded limits");
        }
        let birth = session
            .owned
            .process
            .as_ref()
            .ok_or("Missing helper birth record")?;
        let handle = Handle::open(birth)?.ok_or("create_ap exited")?;
        if !handle.verify(
            &session
                .owned
                .allowed(&self.platform.runtime, self.platform.fixture),
        )? {
            return Err("create_ap exited");
        }
        let ready = self
            .journal
            .lock()
            .await
            .read_private(&session.owned.file("pid.ready"), 32)?
            .ok_or("create_ap is not ready")?;
        if String::from_utf8_lossy(&ready).trim() != birth.pid.to_string() {
            return Err("create_ap readiness identity does not match");
        }
        let dirs = self.directories(&session.owned)?;
        if dirs.len() != 1 {
            return Err("create_ap lacks one owned resource snapshot");
        }
        let (iface, frequency, clients) = self.platform.observe(&session.owned, &dirs[0]).await?;
        if relayforge_core::validate_reply(relayforge_core::Method::GetClients, &clients).is_err() {
            return Err("Invalid create_ap client observation");
        }
        let snapshot = self.platform.radio(&session.owned.parent).await?;
        // Report the band the AP actually broadcasts, never the one that was planned.
        // The frozen radio plan still wins: a hostapd fallback is a fault, because the
        // upstream station would be left on a different channel than the AP.
        let observed = relayforge_core::radio::band(frequency);
        if observed.is_empty() || frequency != session.frequency {
            return Err("Owned AP is not broadcasting on the permitted hotspot channel");
        }
        let caps = snapshot.capabilities;
        if (
            caps.current_sta_ssid,
            caps.current_sta_bssid,
            caps.current_sta_frequency,
        ) != session.baseline
            || !session.baseline.0.is_empty() && iface == session.owned.parent
        {
            return Err("Upstream Wi-Fi changed; sharing must stop");
        }
        session.status.iface = iface;
        session.status.band = observed.into();
        session.status.client_count = clients.as_array().unwrap().len() as u32;
        Ok(clients)
    }
    pub async fn start(
        &self,
        config: &Config,
        mut cancelled: watch::Receiver<u64>,
        expected: Option<&crate::networkmanager::Upstream>,
    ) -> Value {
        if config.get("BACKEND").map(String::as_str) != Some("create_ap") {
            return json!({"success":false,"error":"Native create_ap mode requires BACKEND=create_ap"});
        }
        let mut slot = self.session.lock().await;
        if let Some(session) = slot.as_ref() {
            return if session.status.active && session.status.state == "active" {
                json!({"success":true,"status":session.status})
            } else {
                json!({"success":false,"error":"Stop must confirm create_ap cleanup before restart"})
            };
        }
        if let Err(error) = self.recover().await {
            return json!({"success":false,"error":error});
        }
        // A previous crash must not leave a hostapd process holding this adapter.
        if let Err(error) = self.reap_orphans().await {
            return json!({"success":false,"error":error});
        }
        let gate_cancelled = cancelled.clone();
        let prepare = async {
            if let Some(expected) = expected {
                let fresh =
                    crate::networkmanager::resume_probe(self.platform.fixture, config).await?;
                if *expected != fresh {
                    return Err("Original upstream changed before resume");
                }
            }
            let snapshot = self.platform.radio(&config["WIFI_IFACE"]).await?;
            if expected.is_some_and(|expected| !expected.matches_radio(&snapshot)) {
                return Err("Original upstream changed during resume preflight");
            }
            let plan = snapshot.native_preflight_with_bridge(config, self.allow_bridge)?;
            self.driver_preflight(config)?;
            self.environment_preflight(config, snapshot.phy).await?;
            if config["SHARE_METHOD"] == "bridge" {
                self.bridge_preflight(config).await?;
            }
            let channel = plan.channel.ok_or("Missing permitted create_ap channel")?;
            let frequency = snapshot.capabilities.ap_channels[&plan.band]
                .iter()
                .find(|c| c.channel == channel)
                .ok_or("Missing permitted AP channel")?
                .frequency;
            let caps = snapshot.capabilities;
            let baseline = (
                caps.current_sta_ssid,
                caps.current_sta_bssid,
                caps.current_sta_frequency,
            );
            let mut owned = Owned {
                version: 1,
                token: random_secret()?,
                parent: config["WIFI_IFACE"].clone(),
                helper: self.helper.clone(),
                runner: self.runner.clone(),
                band: plan.band,
                channel,
                process: None,
                bridge: config["SHARE_METHOD"] == "bridge",
            };
            self.journal.lock().await.write(&owned)?;
            let mut snapshot = String::new();
            for (key, value) in config {
                if key != "BACKEND" && key != "FREQ_BAND" && key != "CHANNEL" {
                    snapshot.push_str(&format!("{key}={value}\n"));
                }
            }
            snapshot.push_str(&format!(
                "FREQ_BAND={}\nCHANNEL={}\n",
                owned.band, owned.channel
            ));
            self.journal
                .lock()
                .await
                .create_private(&owned.file("conf"), snapshot.as_bytes())?;
            let mut command = Command::new(&self.runner);
            command
                .args(owned.gate_args(&self.platform.runtime).into_iter().skip(1))
                .env_clear()
                .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
                .env("LC_ALL", "C")
                .env("RUNNING_AS_DAEMON", "1");
            if self.platform.fixture {
                command.env("RELAYFORGE_TEST_STATE", &self.platform.runtime);
            }
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(true);
            command.as_std_mut().process_group(0);
            let mut child = command
                .spawn()
                .map_err(|_| "Cannot start gated create_ap child")?;
            owned.process = Some(Birth::capture(child.id().ok_or("Missing helper PID")?)?);
            self.journal.lock().await.write(&owned)?;
            let log: Tail = Arc::new(std::sync::Mutex::new(Vec::new()));
            let output = vec![
                tokio::spawn(capture(
                    child.stdout.take().unwrap(),
                    Arc::clone(&log),
                )),
                tokio::spawn(capture(
                    child.stderr.take().unwrap(),
                    Arc::clone(&log),
                )),
            ];
            let status = Status {
                backend: "create_ap".into(),
                state: "connecting".into(),
                desired_active: true,
                ssid: config["SSID"].clone(),
                phy_iface: owned.parent.clone(),
                band: owned.band.clone(),
                ..Status::default()
            };
            *slot = Some(Session {
                owned,
                child,
                output,
                log,
                status,
                baseline,
                frequency,
            });
            if gate_cancelled.has_changed().unwrap_or(true) {
                return Err("Startup cancelled");
            }
            let session = slot.as_mut().unwrap();
            let mut input = session.child.stdin.take().ok_or("Missing execution gate")?;
            input
                .write_all(&[1])
                .await
                .map_err(|_| "Cannot acknowledge helper execution")?;
            drop(input);
            let deadline =
                Instant::now() + Duration::from_secs(if self.platform.fixture { 2 } else { 30 });
            loop {
                if session.output.iter().any(|t| t.is_finished())
                    || !session.owned.process.as_ref().unwrap().live()?
                {
                    return Err("create_ap failed during startup");
                }
                if self
                    .journal
                    .lock()
                    .await
                    .read_private(&session.owned.file("pid.ready"), 32)?
                    .is_some()
                {
                    self.verify(session).await?;
                    session.status.active = true;
                    session.status.state = "active".into();
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    return Err("create_ap startup timed out");
                }
                sleep(Duration::from_millis(50)).await;
            }
        };
        let result =
            tokio::select! {result=prepare=>result,_=cancelled.changed()=>Err("Startup cancelled")};
        match result {
            Ok(()) => json!({"success":true,"status":slot.as_ref().unwrap().status}),
            Err(error) => {
                // Publish the helper's explanation before cleanup aborts its capture.
                let reported = slot
                    .as_ref()
                    .is_some_and(|session| report_startup_log(&session.log, config));
                let error = if reported {
                    format!("{error}; see the service journal for create_ap diagnostics")
                } else {
                    error.to_string()
                };
                let cleanup = if let Some(session) = slot.as_mut() {
                    self.cleanup(session).await
                } else {
                    self.recover().await
                };
                if cleanup.is_ok() {
                    *slot = None;
                } else if let Some(session) = slot.as_mut() {
                    session.status.state = "failed".into();
                    session.status.desired_active = false;
                    session.status.error =
                        Some("create_ap cleanup unconfirmed; Stop must retry".into());
                }
                // A failed cleanup must not erase the reason the start failed. The
                // operator needs the original fault as well as the retained journal;
                // reporting only the cleanup state hides the defect that triggered it.
                json!({"success":false,"error":if cleanup.is_ok() {error} else {format!("{error} (cleanup unconfirmed; journal retained)")}})
            }
        }
    }
    pub async fn stop(&self) -> bool {
        let mut slot = self.session.lock().await;
        let result = if let Some(session) = slot.as_mut() {
            self.cleanup(session).await
        } else {
            self.recover().await
        };
        if result.is_ok() {
            *slot = None;
            true
        } else {
            if let Some(s) = slot.as_mut() {
                s.status.state = "failed".into();
                s.status.desired_active = false;
                s.status.error = Some("create_ap cleanup unconfirmed; Stop must retry".into());
            }
            false
        }
    }
    pub async fn poll(&self) -> Option<(Status, Value)> {
        let mut slot = self.session.try_lock().ok()?;
        let session = slot.as_mut()?;
        if !session.status.active {
            return Some((session.status.clone(), json!([])));
        }
        match self.verify(session).await {
            Ok(clients) => Some((session.status.clone(), clients)),
            Err(error) => {
                let clean = self.cleanup(session).await.is_ok();
                let status = Status {
                    active: session.status.active && !clean,
                    desired_active: false,
                    client_count: 0,
                    state: "failed".into(),
                    error: Some(
                        if clean {
                            error
                        } else {
                            "create_ap monitoring failed; cleanup is unconfirmed"
                        }
                        .into(),
                    ),
                    ..session.status.clone()
                };
                if clean {
                    *slot = None;
                } else {
                    session.status = status.clone();
                }
                Some((status, json!([])))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ARP: &str = "IP address       HW type     Flags       HW address            Mask     Device\n\
192.168.12.23    0x1         0x2         aa:bb:cc:dd:ee:01     *        apfake0\n\
192.168.12.24    0x1         0x0         00:00:00:00:00:00     *        apfake0\n\
10.0.0.7         0x1         0x2         AA:BB:CC:DD:EE:02     *        eth0\n\
not-an-ip        0x1         0x2         aa:bb:cc:dd:ee:03     *        apfake0\n\
192.168.12.25    0x1         0x2         garbage               *        apfake0\n\
192.168.12.26    0x1         0x2\n";

    #[test]
    fn neighbor_table_supplies_only_real_completed_addresses() {
        let map = arp_addresses(ARP);
        assert_eq!(map.len(), 2);
        assert_eq!(map["aa:bb:cc:dd:ee:01"], "192.168.12.23");
        assert_eq!(map["aa:bb:cc:dd:ee:02"], "10.0.0.7");
    }

    #[test]
    fn missing_or_partial_neighbor_tables_add_nothing() {
        assert!(arp_addresses("").is_empty());
        assert!(arp_addresses(ARP.lines().next().unwrap()).is_empty());
        assert!(arp_addresses("junk without columns\nmore junk\n").is_empty());
    }

    /// A client with a lease keeps its DHCP identity over a stale ARP entry.
    #[test]
    fn lease_addresses_win_over_neighbor_fallback() {
        let mut addresses = BTreeMap::from([(
            "aa:bb:cc:dd:ee:01".to_owned(),
            ("192.168.12.10".to_owned(), "printer".to_owned()),
        )]);
        for (mac, ip) in arp_addresses(ARP) {
            addresses.entry(mac).or_insert((ip, String::new()));
        }
        assert_eq!(
            addresses["aa:bb:cc:dd:ee:01"],
            ("192.168.12.10".to_owned(), "printer".to_owned())
        );
        assert_eq!(addresses["aa:bb:cc:dd:ee:02"], ("10.0.0.7".to_owned(), String::new()));
    }
}
