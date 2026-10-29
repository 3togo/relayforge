//! Private create_ap snapshot and child recovery protocol, without a Python runtime.
mod daemons;
mod instance_runtime;
mod network;
mod radio_plan;
use crate::{
    ap_firewall::{Executor, Plan, Simulator},
    ap_process::{Birth, Handle},
    config_store::random_secret,
    nm_journal::Result,
    radio,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::CString,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::time::{sleep, Instant};
const LIMIT: usize = 65536;
const FIELDS: &[&str] = &[
    "CONFDIR",
    "COMMON_CONFDIR",
    "WIFI_IFACE",
    "VWIFI_IFACE",
    "INTERNET_IFACE",
    "BRIDGE_IFACE",
    "OLD_MACADDR",
    "NEW_MACADDR",
    "NO_VIRT",
    "SHARE_METHOD",
    "NO_DNS",
    "DNS_PORT",
    "GATEWAY",
    "RUNNING_AS_DAEMON",
    "DAEMON_PIDFILE",
    "ADDED_UNMANAGED",
    "RULE_COMMENT",
    "BRIDGE_CREATED",
    "PHYSICAL_CONFIGURED",
];
const ARRAYS: &[&str] = &["IP_ADDRS", "ROUTE_ADDRS"];
const CHILDREN: &[(&str, &str)] = &[
    ("hostapd", "hostapd"),
    ("dnsmasq", "dnsmasq"),
    ("haveged", "haveged"),
    ("watchdog", "bash"),
];
#[derive(Clone)]
struct Platform {
    root: PathBuf,
    net: PathBuf,
    fixture: Option<PathBuf>,
}
impl Platform {
    fn new(fixture: Option<&Path>) -> Result<Self> {
        if let Some(path) = fixture {
            if unsafe { libc::geteuid() } == 0 {
                return Err("Recovery fixtures require an ordinary user");
            }
            directory(path, true)?;
            Ok(Self {
                root: path.join("engine"),
                net: path.join("net"),
                fixture: Some(path.into()),
            })
        } else {
            if unsafe { libc::geteuid() } != 0 {
                return Err("Recovery helper requires root");
            }
            Ok(Self {
                root: "/run/wifi-relay/create-ap".into(),
                net: "/sys/class/net".into(),
                fixture: None,
            })
        }
    }
    fn open(&self, path: &Path, common: bool) -> Result<Directory> {
        directory(self.root.parent().ok_or("Missing runtime parent")?, true)?;
        directory(&self.root, true)?;
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("Invalid recovery directory")?;
        if path.parent() != Some(self.root.as_path())
            || !(instance(name) || common && name == "create_ap.common.conf")
        {
            return Err("Recovery directory is outside the private engine runtime");
        }
        Directory::open(path)
    }
    fn interface(&self, iface: &str) -> Result<Option<Interface>> {
        interface_name(iface)?;
        let net = self.net.join(iface);
        let index = match fs::read_to_string(net.join("ifindex")) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err("Cannot inspect recovery interface"),
        };
        let mac = fs::read_to_string(net.join("address"))
            .map_err(|_| "Cannot inspect recovery interface MAC")?
            .trim()
            .to_owned();
        valid_mac(&mac)?;
        let ifindex = index
            .trim()
            .parse::<u32>()
            .ok()
            .filter(|i| *i > 0)
            .ok_or("Invalid interface index")?;
        Ok(Some(Interface { ifindex, mac }))
    }
}
fn directory(path: &Path, private: bool) -> Result<()> {
    let meta = fs::symlink_metadata(path).map_err(|_| "Cannot inspect recovery directory")?;
    if !meta.is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || if private {
            meta.mode() & 0o7777 != 0o700
        } else {
            meta.mode() & 0o022 != 0
        }
    {
        return Err("Unsafe recovery directory");
    }
    Ok(())
}
fn interface_name(value: &str) -> Result<()> {
    relayforge_core::config::parse_patch(&serde_json::json!({"WIFI_IFACE":value}).to_string())
        .map(|_| ())
}
fn instance(name: &str) -> bool {
    name.strip_prefix("create_ap.")
        .and_then(|n| n.rsplit_once(".conf."))
        .is_some_and(|(iface, tail)| {
            interface_name(iface).is_ok()
                && tail.len() == 8
                && tail.bytes().all(|b| b.is_ascii_alphanumeric())
        })
}
fn valid_mac(value: &str) -> Result<()> {
    if value.split(':').collect::<Vec<_>>().len() != 6
        || !value
            .split(':')
            .all(|v| v.len() == 2 && v.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return Err("Invalid recovery MAC");
    }
    Ok(())
}
struct Directory {
    path: PathBuf,
    fd: File,
}
impl Directory {
    fn open(path: &Path) -> Result<Self> {
        directory(path, false)?;
        let fd = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "Cannot open recovery directory")?;
        let meta = fd
            .metadata()
            .map_err(|_| "Cannot inspect recovery directory")?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o022 != 0 {
            return Err("Unsafe recovery directory");
        }
        Ok(Self {
            path: path.into(),
            fd,
        })
    }
    fn read(&self, name: &str, private: bool, limit: usize) -> Result<Option<Vec<u8>>> {
        let name = filename(name)?;
        let fd = unsafe {
            libc::openat(
                self.fd.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                Ok(None)
            } else {
                Err("Cannot read recovery file")
            };
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let m = file
            .metadata()
            .map_err(|_| "Cannot inspect recovery file")?;
        if !m.is_file()
            || m.uid() != unsafe { libc::geteuid() }
            || m.nlink() != 1
            || if private {
                m.mode() & 0o7777 != 0o600
            } else {
                m.mode() & 0o022 != 0
            }
            || m.len() > limit as u64
        {
            return Err("Unsafe or oversized recovery file");
        }
        let mut bytes = Vec::new();
        file.take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read recovery data")?;
        if bytes.len() > limit {
            return Err("Oversized recovery data");
        }
        Ok(Some(bytes))
    }
    fn write<T: Serialize>(&self, name: &str, value: &T, expected: Option<&[u8]>) -> Result<()> {
        let bytes = serde_json::to_vec(value).map_err(|_| "Cannot serialize recovery data")?;
        self.write_bytes(name, &bytes, expected)
    }
    fn write_bytes(&self, name: &str, bytes: &[u8], expected: Option<&[u8]>) -> Result<()> {
        if bytes.len() > LIMIT {
            return Err("Oversized recovery data");
        }
        if self.read(name, true, LIMIT)?.as_deref() != expected {
            return Err("Recovery file changed externally");
        }
        let temporary = filename(&format!(".runtime-{}", random_secret()?))?;
        let target = filename(name)?;
        let fd = unsafe {
            libc::openat(
                self.fd.as_raw_fd(),
                temporary.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("Cannot create private recovery file");
        }
        let mut file = unsafe { File::from_raw_fd(fd) };
        let result = (|| {
            file.write_all(bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| "Cannot persist recovery data")?;
            if self.read(name, true, LIMIT)?.as_deref() != expected {
                return Err("Recovery file changed externally");
            }
            if unsafe {
                libc::renameat(
                    self.fd.as_raw_fd(),
                    temporary.as_ptr(),
                    self.fd.as_raw_fd(),
                    target.as_ptr(),
                )
            } != 0
            {
                return Err("Cannot commit recovery data");
            }
            self.fd
                .sync_all()
                .map_err(|_| "Cannot sync recovery directory")
        })();
        unsafe {
            libc::unlinkat(self.fd.as_raw_fd(), temporary.as_ptr(), 0);
        }
        result
    }
    fn entries(&self) -> Result<Vec<String>> {
        let mut names = Vec::new();
        for e in fs::read_dir(&self.path).map_err(|_| "Cannot list recovery files")? {
            let name = e
                .map_err(|_| "Cannot list recovery files")?
                .file_name()
                .into_string()
                .map_err(|_| "Invalid recovery filename")?;
            if name.ends_with(".pid") || name.ends_with(".identity") {
                let stem = name.rsplit_once('.').unwrap().0;
                if !CHILDREN.iter().any(|(s, _)| *s == stem) {
                    return Err("Unknown child record; refusing cleanup");
                }
                names.push(name);
            }
        }
        names.sort();
        Ok(names)
    }
}
struct TransactionLock(File);
impl Drop for TransactionLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}
impl Directory {
    async fn firewall_lock(&self) -> Result<TransactionLock> {
        self.transaction_lock(c".firewall.lock").await
    }
    async fn transaction_lock(&self, name: &std::ffi::CStr) -> Result<TransactionLock> {
        let fd = unsafe {
            libc::openat(
                self.fd.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDWR
                    | libc::O_CREAT
                    | libc::O_NOFOLLOW
                    | libc::O_NONBLOCK
                    | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("Cannot open runtime transaction lock");
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let m = file
            .metadata()
            .map_err(|_| "Cannot inspect runtime transaction lock")?;
        if !m.is_file()
            || m.uid() != unsafe { libc::geteuid() }
            || m.nlink() != 1
            || m.mode() & 0o7777 != 0o600
        {
            return Err("Unsafe runtime transaction lock");
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                return Ok(TransactionLock(file));
            }
            let errno = std::io::Error::last_os_error().raw_os_error();
            if errno != Some(libc::EWOULDBLOCK) {
                return Err("Cannot lock runtime transaction");
            }
            if Instant::now() >= deadline {
                return Err("Runtime transaction still running; cleanup is unconfirmed");
            }
            sleep(Duration::from_millis(20)).await;
        }
    }
}
struct Firewall<'a> {
    platform: &'a Platform,
    data: &'a Snapshot,
    firewalld: bool,
}

/// When firewalld owns the host firewall, raw iptables mutations are flushed on
/// the next firewalld reload. Route the same owned rules through firewalld's
/// direct passthrough so they are tracked by the distribution policy instead of
/// being clobbered. This never performs a global firewalld rewrite.
fn firewalld_active() -> bool {
    for path in [
        "/usr/bin/firewall-cmd",
        "/usr/sbin/firewall-cmd",
        "/bin/firewall-cmd",
        "/sbin/firewall-cmd",
    ] {
        if let Ok(output) = std::process::Command::new(path)
            .arg("--state")
            .output()
        {
            if output.status.success()
                && String::from_utf8_lossy(&output.stdout).trim() == "running"
            {
                return true;
            }
        }
    }
    false
}

/// Wrap iptables arguments so they are applied through firewalld's direct
/// passthrough, keeping them under distribution firewall management.
fn firewalld_passthrough(args: &[String]) -> Vec<String> {
    let mut command = vec![
        "--direct".into(),
        "--passthrough".into(),
        "ipv4".into(),
    ];
    command.extend(args.iter().cloned());
    command
}
impl Firewall<'_> {
    fn permit_add(&self) -> Result<()> {
        if self.data.owner.current()?.is_none() {
            return Err("Firewall owner exited; refusing insertion");
        }
        let f = &self.data.fields;
        let iface = &f["WIFI_IFACE"];
        if (f["NO_VIRT"] == "0" && (f["VWIFI_IFACE"].is_empty() || f["VWIFI_IFACE"] != *iface))
            || (f["NO_VIRT"] == "1" && f["PHYSICAL_CONFIGURED"] != "1")
            || !self.data.interfaces.contains_key(iface)
            || self.platform.interface(iface)?.is_none()
        {
            return Err("Firewall AP identity is not realized and recorded");
        }
        Ok(())
    }
}
impl Executor for Firewall<'_> {
    async fn execute(&mut self, args: &[String]) -> Result<radio::Reply> {
        let operation = args.get(4).map(String::as_str);
        if matches!(operation, Some("-I" | "-D")) {
            self.data.interfaces(self.platform)?;
        }
        if operation == Some("-I") {
            self.permit_add()?;
        }
        if let Some(root) = &self.platform.fixture {
            let dir = Directory::open(root)?;
            let previous = dir.read("firewall-state.json", true, LIMIT)?;
            if let Some(bytes) = previous {
                let mut state: Simulator = serde_json::from_slice(&bytes)
                    .map_err(|_| "Invalid simulated firewall state")?;
                let reply = state.execute(args)?;
                dir.write("firewall-state.json", &state, Some(&bytes))?;
                return Ok(reply);
            }
            // Older recovery fixtures supply read-only listings, never a system tool.
            if operation != Some("-S") {
                return Err("Missing mutable simulated firewall state");
            }
            let bytes = dir
                .read("firewall.json", true, LIMIT)?
                .ok_or("Missing simulated firewall")?;
            let map: BTreeMap<String, String> =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid simulated firewall")?;
            let table = args.get(3).ok_or("Missing simulated firewall table")?;
            return Ok(radio::Reply {
                code: Some(0),
                stdout: map
                    .get(table)
                    .ok_or("Missing simulated firewall table")?
                    .clone(),
            });
        }
        let environment = BTreeMap::from([
            ("PATH".into(), "/usr/sbin:/usr/bin:/sbin:/bin".into()),
            ("LC_ALL".into(), "C".into()),
        ]);
        if self.firewalld {
            let program = [
                "/usr/bin/firewall-cmd",
                "/usr/sbin/firewall-cmd",
                "/bin/firewall-cmd",
                "/sbin/firewall-cmd",
            ]
            .into_iter()
            .map(Path::new)
            .find(|p| p.is_file())
            .ok_or("firewall-cmd unavailable")?;
            return radio::command_result(
                program,
                &firewalld_passthrough(args)
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                Duration::from_secs(4),
                Some(&environment),
            )
            .await
            .map_err(|_| "Firewall command failed or timed out");
        }
        let program = [
            "/usr/sbin/iptables",
            "/usr/bin/iptables",
            "/sbin/iptables",
            "/bin/iptables",
        ]
        .into_iter()
        .map(Path::new)
        .find(|p| p.is_file())
        .ok_or("iptables unavailable")?;
        radio::command_result(
            program,
            &args.iter().map(String::as_str).collect::<Vec<_>>(),
            Duration::from_secs(4),
            Some(&environment),
        )
        .await
        .map_err(|_| "Firewall command failed or timed out")
    }
}
impl Directory {
    fn remove(&self, name: &str, expected: &[u8]) -> Result<()> {
        if self.read(name, true, LIMIT)?.as_deref() != Some(expected) {
            return Err("Network journal changed externally");
        }
        let name = filename(name)?;
        if unsafe { libc::unlinkat(self.fd.as_raw_fd(), name.as_ptr(), 0) } != 0 {
            return Err("Cannot remove network journal");
        }
        self.fd
            .sync_all()
            .map_err(|_| "Cannot sync network journal removal")
    }
}
fn filename(value: &str) -> Result<CString> {
    if value.is_empty() || value == "." || value == ".." || value.contains('/') {
        return Err("Invalid recovery filename");
    }
    CString::new(value).map_err(|_| "Invalid recovery filename")
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Identity {
    pid: u32,
    starttime: String,
    cmdline_sha256: String,
    exe: String,
}
struct Observed {
    uid: u32,
    identity: Identity,
    birth: Birth,
    args: Vec<String>,
}
fn observe(pid: u32) -> Result<Option<Observed>> {
    if pid <= 1 || pid > i32::MAX as u32 {
        return Err("Invalid child PID");
    }
    let birth = match Birth::capture(pid) {
        Ok(v) => v,
        Err(e) => {
            // Only absence/zombie means exited. Permission and format errors stay errors.
            let path = PathBuf::from(format!("/proc/{pid}"));
            if !path.exists() {
                return Ok(None);
            }
            let stat = fs::read_to_string(path.join("stat")).map_err(|_| e)?;
            if stat
                .rsplit_once(')')
                .is_some_and(|(_, s)| s.split_whitespace().next() == Some("Z"))
            {
                return Ok(None);
            }
            return Err(e);
        }
    };
    let path = PathBuf::from(format!("/proc/{pid}"));
    let uid = fs::metadata(&path)
        .map_err(|_| "Cannot inspect child user")?
        .uid();
    let exe = fs::read_link(path.join("exe"))
        .map_err(|_| "Cannot inspect child executable")?
        .to_str()
        .ok_or("Invalid child executable")?
        .trim_end_matches(" (deleted)")
        .to_owned();
    let mut bytes = Vec::new();
    File::open(path.join("cmdline"))
        .and_then(|f| f.take(16385).read_to_end(&mut bytes))
        .map_err(|_| "Cannot inspect child arguments")?;
    if bytes.len() > 16384 || bytes.is_empty() {
        return Err("Invalid child arguments");
    }
    let hash = format!("{:x}", Sha256::digest(&bytes));
    let text = String::from_utf8(bytes).map_err(|_| "Invalid child arguments")?;
    let args = text
        .trim_end_matches('\0')
        .split('\0')
        .map(str::to_owned)
        .collect();
    if !birth.live()? {
        return Ok(None);
    }
    Ok(Some(Observed {
        uid,
        identity: Identity {
            pid,
            starttime: birth.start.clone(),
            cmdline_sha256: hash,
            exe,
        },
        birth,
        args,
    }))
}
impl Identity {
    fn validate(&self) -> Result<()> {
        if self.pid <= 1
            || self.pid > i32::MAX as u32
            || self.starttime.is_empty()
            || self.starttime.len() > 24
            || !self.starttime.bytes().all(|b| b.is_ascii_digit())
            || self.cmdline_sha256.len() != 64
            || !self.cmdline_sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || !Path::new(&self.exe).is_absolute()
        {
            return Err("Invalid child identity record");
        }
        Ok(())
    }
    fn current(&self) -> Result<Option<Observed>> {
        self.validate()?;
        Ok(observe(self.pid)?.filter(|v| v.identity == *self))
    }
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Interface {
    ifindex: u32,
    mac: String,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DirectoryIdentity {
    device: u64,
    inode: u64,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    directory: Option<DirectoryIdentity>,
    owner: Identity,
    fields: BTreeMap<String, String>,
    arrays: BTreeMap<String, Vec<String>>,
    interfaces: BTreeMap<String, Interface>,
}
impl Snapshot {
    fn bind_directory(&self, dir: &Directory) -> Result<()> {
        if let Some(expected) = &self.directory {
            let meta = dir
                .fd
                .metadata()
                .map_err(|_| "Cannot inspect instance directory identity")?;
            if meta.dev() != expected.device || meta.ino() != expected.inode {
                return Err("Instance directory identity changed; refusing recovery");
            }
        }
        Ok(())
    }
    fn validate(&self, p: &Platform, path: &Path) -> Result<()> {
        self.owner.validate()?;
        if self.fields.keys().any(|k| !FIELDS.contains(&k.as_str()))
            || self.arrays.keys().any(|k| !ARRAYS.contains(&k.as_str()))
            || self.fields.get("CONFDIR").map(Path::new) != Some(path)
            || self.fields.get("COMMON_CONFDIR").map(Path::new)
                != Some(p.root.join("create_ap.common.conf").as_path())
        {
            return Err("Invalid recovery snapshot schema");
        }
        for (key, v) in &self.fields {
            if v.contains('\0') || v.len() > 4096 {
                return Err("Invalid recovery field");
            }
            match key.as_str() {
                "WIFI_IFACE" | "VWIFI_IFACE" | "INTERNET_IFACE" | "BRIDGE_IFACE" => {
                    if !v.is_empty() {
                        interface_name(v)?;
                    }
                }
                "OLD_MACADDR" | "NEW_MACADDR" => {
                    if !v.is_empty() {
                        valid_mac(v)?;
                    }
                }
                "NO_VIRT"
                | "NO_DNS"
                | "RUNNING_AS_DAEMON"
                | "BRIDGE_CREATED"
                | "PHYSICAL_CONFIGURED" => {
                    if !["0", "1"].contains(&v.as_str()) {
                        return Err("Invalid recovery flag");
                    }
                }
                "SHARE_METHOD" => {
                    if !["nat", "none", "bridge"].contains(&v.as_str()) {
                        return Err("Invalid recovery sharing method");
                    }
                }
                "DNS_PORT" => {
                    if !v.is_empty() && v.parse::<u16>().is_err() {
                        return Err("Invalid recovery DNS port");
                    }
                }
                "GATEWAY" => {
                    if v.parse::<std::net::Ipv4Addr>().is_err() {
                        return Err("Invalid recovery gateway");
                    }
                }
                "ADDED_UNMANAGED" => {
                    for iface in v.split_whitespace() {
                        interface_name(iface)?;
                    }
                }
                "RULE_COMMENT" => {
                    if v != &format!("wifi-relay-{}", path.file_name().unwrap().to_str().unwrap()) {
                        return Err("Invalid recovery firewall identity");
                    }
                }
                "DAEMON_PIDFILE" => {
                    let dir = if let Some(root) = &p.fixture {
                        root.clone()
                    } else {
                        PathBuf::from("/run/relayforge")
                    };
                    let marker = Path::new(v);
                    let token = marker
                        .file_name()
                        .and_then(|s| s.to_str())
                        .and_then(|s| s.strip_prefix("create-ap-"))
                        .and_then(|s| s.strip_suffix(".pid"));
                    if marker.parent() != Some(dir.as_path())
                        || !token.is_some_and(|t| {
                            t.len() == 32 && t.bytes().all(|b| b.is_ascii_hexdigit())
                        })
                    {
                        return Err("Invalid native pidfile marker");
                    }
                }
                _ => {}
            }
        }
        for (iface, identity) in &self.interfaces {
            interface_name(iface)?;
            valid_mac(&identity.mac)?;
            if identity.ifindex == 0 {
                return Err("Invalid recovery interface index");
            }
        }
        if self
            .arrays
            .values()
            .flatten()
            .any(|v| v.contains('\0') || v.len() > 4096)
        {
            return Err("Invalid recovery array");
        }
        // All shell cleanup variables must be present, even before their resources exist.
        if FIELDS.iter().any(|k| !self.fields.contains_key(*k)) {
            return Err("Incomplete recovery snapshot");
        }
        Ok(())
    }
    fn interfaces(&self, p: &Platform) -> Result<()> {
        for key in ["VWIFI_IFACE", "BRIDGE_IFACE"] {
            let name = &self.fields[key];
            if !name.is_empty() && !self.interfaces.contains_key(name) {
                if let Some(current) = p.interface(name)? {
                    if key != "VWIFI_IFACE"
                        || !current
                            .mac
                            .eq_ignore_ascii_case(&self.fields["NEW_MACADDR"])
                    {
                        return Err("Unrecorded interface identity; refusing recovery");
                    }
                }
            }
        }
        for (name, expected) in &self.interfaces {
            if let Some(current) = p.interface(name)? {
                let owned =
                    name == &self.fields["VWIFI_IFACE"] || name == &self.fields["BRIDGE_IFACE"];
                if current.ifindex != expected.ifindex
                    || owned && !current.mac.eq_ignore_ascii_case(&expected.mac)
                {
                    return Err("Recovery interface identity changed");
                }
            }
        }
        Ok(())
    }
}
fn snapshot(p: &Platform, dir: &Directory) -> Result<(Snapshot, Vec<u8>)> {
    let bytes = dir
        .read("recovery.json", true, LIMIT)?
        .ok_or("Missing recovery snapshot")?;
    let data: Snapshot =
        serde_json::from_slice(&bytes).map_err(|_| "Invalid recovery snapshot JSON")?;
    data.validate(p, &dir.path)?;
    data.bind_directory(dir)?;
    Ok((data, bytes))
}
fn pid(dir: &Directory, stem: &str) -> Result<Option<u32>> {
    let Some(bytes) = dir.read(&format!("{stem}.pid"), false, 32)? else {
        return Ok(None);
    };
    let pid = std::str::from_utf8(&bytes)
        .map_err(|_| "Invalid child pidfile")?
        .trim()
        .parse::<u32>()
        .map_err(|_| "Invalid child pidfile")?;
    if pid <= 1 || pid > i32::MAX as u32 {
        return Err("Invalid child pidfile PID");
    }
    Ok(Some(pid))
}
fn recording_uid(expected: &str, service_uid: u32, child_uid: u32) -> bool {
    service_uid == child_uid || service_uid == 0 && expected == "dnsmasq"
}
async fn record(p: &Platform, dir: &Directory, stem: &str, expected: &str) -> Result<()> {
    if !CHILDREN.contains(&(stem, expected)) {
        return Err("Unsupported child executable");
    }
    let child = pid(dir, stem)?.ok_or("Missing child pidfile")?;
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        let observed = observe(child)?.ok_or("Child exited before its identity was recorded")?;
        if !recording_uid(expected, unsafe { libc::geteuid() }, observed.uid) {
            return Err("Child user does not match its expected privilege policy");
        }
        if Path::new(&observed.identity.exe)
            .file_name()
            .and_then(|s| s.to_str())
            == Some(expected)
        {
            if expected == "bash" {
                let (data, _) = snapshot(p, dir)?;
                if !data
                    .owner
                    .current()?
                    .is_some_and(|v| v.uid == unsafe { libc::geteuid() })
                {
                    return Err("Cannot record a watchdog without its live owner");
                }
                let stat = fs::read_to_string(format!("/proc/{child}/stat"))
                    .map_err(|_| "Cannot inspect watchdog parent")?;
                let ppid = stat
                    .rsplit_once(')')
                    .and_then(|(_, r)| r.split_whitespace().nth(1))
                    .and_then(|s| s.parse::<u32>().ok());
                if ppid != Some(data.owner.pid) {
                    return Err("Watchdog is not a direct child of this owner");
                }
            } else {
                let prefix = format!("{}/", dir.path.display());
                if !observed.args.iter().any(|a| a.contains(&prefix)) {
                    return Err("Child does not use this instance's private configuration");
                }
            }
            let name = format!("{stem}.identity");
            let previous = dir.read(&name, true, LIMIT)?;
            if let Some(bytes) = &previous {
                let prior: Identity =
                    serde_json::from_slice(bytes).map_err(|_| "Invalid child identity JSON")?;
                if prior != observed.identity && prior.current()?.is_some() {
                    return Err("Refusing to replace a live child identity");
                }
            }
            return dir.write(&name, &observed.identity, previous.as_deref());
        }
        if Instant::now() >= deadline {
            return Err("Child executable does not match its pidfile");
        }
        sleep(Duration::from_millis(10)).await;
    }
}
async fn stop(identity: &Identity) -> Result<()> {
    let Some(observed) = identity.current()? else {
        return Ok(());
    };
    let Some(handle) = Handle::open(&observed.birth)? else {
        return Ok(());
    };
    let allowed = [(identity.exe.clone(), observed.args)];
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        let Some(current) = identity.current()? else {
            return Ok(());
        };
        handle.signal_as(signal, &allowed, current.uid)?;
        let deadline = Instant::now() + Duration::from_secs(2);
        while !handle.exited()? && Instant::now() < deadline {
            sleep(Duration::from_millis(20)).await;
        }
        if handle.exited()? {
            return Ok(());
        }
    }
    Err("Owned child did not exit")
}
async fn stop_children(p: &Platform, dir: &Directory) -> Result<()> {
    let mut failure = daemons::stop_all(p, dir).await.err();
    dir.entries()?;
    // Quiesce every verified child even if another record prevents full cleanup.
    for (stem, exe) in CHILDREN {
        let result = async {
            let name = format!("{stem}.identity");
            if dir.read(&name, true, LIMIT)?.is_none() {
                if let Some(pid) = pid(dir, stem)? {
                    if observe(pid)?.is_some() {
                        if *stem == "watchdog" {
                            return Err("Unrecorded watchdog; refusing to signal a bare PID");
                        }
                        record(p, dir, stem, exe).await?;
                    }
                }
            }
            if let Some(bytes) = dir.read(&name, true, LIMIT)? {
                let identity: Identity =
                    serde_json::from_slice(&bytes).map_err(|_| "Invalid child identity JSON")?;
                stop(&identity).await?;
            }
            Ok(())
        }
        .await;
        if let Err(error) = result {
            failure.get_or_insert(error);
        }
    }
    failure.map_or(Ok(()), Err)
}

fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
async fn action(p: &Platform, action: &str, path: &Path, values: &[String]) -> Result<String> {
    let parent = if action == "record-child" {
        path.parent().ok_or("Missing child directory")?
    } else {
        path
    };
    let dir = p.open(parent, matches!(action, "record-child" | "stop-children"))?;
    match action {
        "radio-check" if values.is_empty() => {
            radio_plan::check(p, &dir, true).await?;
            Ok(String::new())
        }
        "daemons-configure" if values.len() == 3 => daemons::configure(p, &dir, values).await,
        "daemons-leases" if values.is_empty() => String::from_utf8(daemons::read_leases(p, &dir)?)
            .map_err(|_| "Invalid DNS lease encoding"),
        "daemons-run" if values.is_empty() => {
            daemons::run(p, &dir).await?;
            Ok(String::new())
        }
        "daemon-exec" if values.len() == 1 => {
            daemons::exec(p, &dir, &values[0]).await?;
            Ok(String::new())
        }
        "save" => {
            let (pid, values) = values.split_first().ok_or("Missing owner PID")?;
            let observed =
                observe(pid.parse().map_err(|_| "Invalid owner PID")?)?.ok_or("Owner exited")?;
            if observed.uid != unsafe { libc::geteuid() } {
                return Err("Recovery owner has a different user");
            }
            let owner = observed.identity;
            let previous = dir.read("recovery.json", true, LIMIT)?;
            let mut data = Snapshot {
                directory: None,
                owner,
                fields: BTreeMap::new(),
                arrays: ARRAYS.iter().map(|k| (k.to_string(), Vec::new())).collect(),
                interfaces: BTreeMap::new(),
            };
            if let Some(bytes) = &previous {
                let old: Snapshot =
                    serde_json::from_slice(bytes).map_err(|_| "Invalid prior recovery snapshot")?;
                old.validate(p, parent)?;
                old.bind_directory(&dir)?;
                data.directory = old.directory.clone();
                if old.owner != data.owner {
                    return Err("Recovery snapshot owner changed");
                }
                old.interfaces(p)?;
                data.interfaces = old.interfaces;
            }
            for value in values {
                let (k, v) = value.split_once('=').ok_or("Malformed recovery field")?;
                if let Some(key) = k.strip_suffix("[]").filter(|k| ARRAYS.contains(k)) {
                    data.arrays.get_mut(key).unwrap().push(v.into());
                } else if FIELDS.contains(&k) && data.fields.insert(k.into(), v.into()).is_none() {
                } else {
                    return Err("Unsupported or repeated recovery field");
                }
            }
            data.validate(p, parent)?;
            for key in [
                "WIFI_IFACE",
                "VWIFI_IFACE",
                "INTERNET_IFACE",
                "BRIDGE_IFACE",
            ] {
                let iface = &data.fields[key];
                if !iface.is_empty() {
                    if let Some(current) = p.interface(iface)? {
                        data.interfaces.insert(iface.clone(), current);
                    }
                }
            }
            data.interfaces(p)?;
            dir.write("recovery.json", &data, previous.as_deref())?;
            Ok(String::new())
        }
        "load" if values.is_empty() => {
            let (data, _) = snapshot(p, &dir)?;
            if data.owner.current()?.is_some() {
                return Err("Refusing recovery while its owner is alive");
            }
            stop_children(p, &dir).await?;
            data.interfaces(p)?;
            let mut lines: Vec<_> = data
                .fields
                .iter()
                .map(|(k, v)| format!("{k}={}", quote(v)))
                .collect();
            lines.extend(data.arrays.iter().map(|(k, vs)| {
                format!(
                    "{k}=({})",
                    vs.iter().map(|v| quote(v)).collect::<Vec<_>>().join(" ")
                )
            }));
            Ok(lines.join("\n"))
        }
        "record-child" if values.len() == 1 => {
            let name = path
                .file_name()
                .and_then(|v| v.to_str())
                .and_then(|n| n.strip_suffix(".pid"))
                .ok_or("Invalid child pidfile name")?;
            record(p, &dir, name, &values[0]).await?;
            Ok(String::new())
        }
        "stop-children" if values.is_empty() => {
            stop_children(p, &dir).await?;
            Ok(String::new())
        }
        "check-interfaces" if values.is_empty() => {
            snapshot(p, &dir)?.0.interfaces(p)?;
            Ok(String::new())
        }
        "firewall-add" if values.len() == 1 => {
            let _lock = dir.firewall_lock().await?;
            let (data, _) = snapshot(p, &dir)?;
            data.interfaces(p)?;
            Firewall {
                platform: p,
                data: &data,
                firewalld: firewalld_active(),
            }
            .permit_add()?;
            let plan = Plan::new(&data.fields)?;
            tokio::time::timeout(
                Duration::from_secs(12),
                plan.apply(
                    &values[0],
                    &mut Firewall {
                        platform: p,
                        data: &data,
                        firewalld: firewalld_active(),
                    },
                ),
            )
            .await
            .map_err(|_| "Firewall application deadline exceeded; cleanup required")??;
            Ok(String::new())
        }
        "firewall-remove" | "firewall-check" if values.is_empty() => {
            let _lock = dir.firewall_lock().await?;
            let (data, _) = snapshot(p, &dir)?;
            data.interfaces(p)?;
            let plan = Plan::new(&data.fields)?;
            let mut driver = Firewall {
                platform: p,
                data: &data,
                firewalld: firewalld_active(),
            };
            tokio::time::timeout(Duration::from_secs(12), async {
                if action == "firewall-remove" {
                    plan.remove(&mut driver).await
                } else {
                    plan.verify(&mut driver).await
                }
            })
            .await
            .map_err(|_| "Firewall cleanup deadline exceeded; snapshot retained")??;
            Ok(String::new())
        }
        "interface-create" | "device-fields" | "device-release" | "device-restore"
            if values.is_empty() =>
        {
            tokio::time::timeout(
                Duration::from_secs(12),
                network::device_action(p, &dir, action),
            )
            .await
            .map_err(|_| "Device operation timed out; intent retained")?
        }
        "interface-configure"
        | "interface-remove"
        | "forwarding-enable"
        | "forwarding-release"
        | "network-check"
            if values.is_empty() =>
        {
            tokio::time::timeout(Duration::from_secs(12), network::run(p, &dir, action))
                .await
                .map_err(|_| "Network transaction timed out; journal retained")??;
            Ok(String::new())
        }
        "verify-cleanup" if values.is_empty() => {
            let (data, _) = snapshot(p, &dir)?;
            daemons::check(p, &dir)?;
            network::run(p, &dir, "network-check").await?;
            for name in dir.entries()? {
                if let Some(stem) = name.strip_suffix(".pid") {
                    if dir
                        .read(&format!("{stem}.identity"), true, LIMIT)?
                        .is_none()
                    {
                        if let Some(pid) = pid(&dir, stem)? {
                            if observe(pid)?.is_some() {
                                return Err("Unverified child survived cleanup");
                            }
                        }
                    }
                } else if let Some(bytes) = dir.read(&name, true, LIMIT)? {
                    let identity: Identity = serde_json::from_slice(&bytes)
                        .map_err(|_| "Invalid child identity JSON")?;
                    if identity.current()?.is_some() {
                        return Err("Owned child survived cleanup");
                    }
                }
            }
            for key in ["VWIFI_IFACE", "BRIDGE_IFACE"] {
                let iface = &data.fields[key];
                if !iface.is_empty()
                    && !(key == "BRIDGE_IFACE" && iface == &data.fields["INTERNET_IFACE"])
                    && p.interface(iface)?.is_some()
                {
                    return Err("Owned interface survived cleanup");
                }
            }
            let _lock = dir.firewall_lock().await?;
            Plan::new(&data.fields)?
                .verify(&mut Firewall {
                    platform: p,
                    data: &data,
                    firewalld: firewalld_active(),
                })
                .await?;
            Ok(String::new())
        }
        _ => Err("Invalid create_ap runtime action or arguments"),
    }
}
pub async fn run(args: &[String], fixture: bool) -> Result<String> {
    let (platform, args) = if fixture {
        let (root, rest) = args.split_first().ok_or("Missing fixture root")?;
        (Platform::new(Some(Path::new(root)))?, rest)
    } else {
        (Platform::new(None)?, args)
    };
    let [name, path, values @ ..] = args else {
        return Err("Missing create_ap runtime action/path");
    };
    if values.iter().map(String::len).sum::<usize>() > LIMIT {
        return Err("Recovery arguments exceed limit");
    }
    action(&platform, name, Path::new(path), values).await
}
pub(crate) fn daemon_leases(path: &Path, fixture: Option<&Path>) -> Result<Vec<u8>> {
    let platform = Platform::new(fixture)?;
    let dir = platform.open(path, false)?;
    daemons::read_leases(&platform, &dir)
}

pub async fn run_instance(args: &[String], fixture: bool) -> Result<()> {
    let (platform, args) = if fixture {
        let (root, args) = args.split_first().ok_or("Missing fixture root")?;
        (Platform::new(Some(Path::new(root)))?, args)
    } else {
        (Platform::new(None)?, args)
    };
    instance_runtime::run(&platform, args).await
}

pub(crate) async fn finish_instance_cleanup(marker: &Path, fixture: Option<&Path>) -> Result<()> {
    let platform = Platform::new(fixture)?;
    instance_runtime::completion::recover_marker(&platform, marker).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn privilege_drop_policy_only_extends_root_dnsmasq_recording() {
        assert!(recording_uid("dnsmasq", 0, 65534));
        assert!(recording_uid("hostapd", 1000, 1000));
        assert!(!recording_uid("hostapd", 0, 65534));
        assert!(!recording_uid("bash", 0, 65534));
        assert!(!recording_uid("dnsmasq", 1000, 1001));
    }
    #[test]
    fn quoting_and_instance_paths_are_literal() {
        assert_eq!(quote("a'$(id)\n"), "'a'\"'\"'$(id)\n'");
        assert!(instance("create_ap.wlp2s0.conf.Ab123456"));
        assert!(!instance("create_ap.-unsafe.conf.Ab123456"));
        assert!(!instance("create_ap.wlp2s0.conf.Ab12345/"));
    }
    #[test]
    fn firewalld_passthrough_wraps_owned_iptables_rules() {
        let args = vec![
            "-w".into(),
            "2".into(),
            "-t".into(),
            "nat".into(),
            "-I".into(),
            "POSTROUTING".into(),
        ];
        let wrapped = firewalld_passthrough(&args);
        assert_eq!(
            wrapped,
            vec![
                "--direct".to_string(),
                "--passthrough".to_string(),
                "ipv4".to_string(),
                "-w".to_string(),
                "2".to_string(),
                "-t".to_string(),
                "nat".to_string(),
                "-I".to_string(),
                "POSTROUTING".to_string(),
            ]
        );
    }
}
