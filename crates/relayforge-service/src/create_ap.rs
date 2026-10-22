//! Native create_ap supervision. The immutable Bash helper retains network transactions.
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
        vec![
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
        ]
    }
    fn gate_args(&self, root: &Path) -> Vec<String> {
        vec![
            self.runner.clone(),
            "--create-ap-exec".into(),
            self.helper.clone(),
            self.config(root).to_string_lossy().into(),
            self.pidfile(root).to_string_lossy().into(),
            self.band.clone(),
            self.channel.to_string(),
        ]
    }
    fn allowed(&self, root: &Path) -> Vec<(String, Vec<String>)> {
        let bash = fs::canonicalize("/bin/bash")
            .unwrap_or_else(|_| PathBuf::from("/usr/bin/bash"))
            .to_string_lossy()
            .into_owned();
        let mut args = vec![bash.clone()];
        args.extend(self.args(root));
        vec![(self.runner.clone(), self.gate_args(root)), (bash, args)]
    }
}
/// The child cannot exec the network helper until the parent durably records its birth.
pub fn exec_gate(args: &[String]) -> std::result::Result<(), Box<dyn std::error::Error>> {
    if args.len() != 6 {
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
    };
    if !["2.4", "5"].contains(&owned.band.as_str()) || !(1..=196).contains(&owned.channel) {
        return Err("Invalid create_ap radio plan".into());
    }
    let bash = fs::canonicalize("/bin/bash")?;
    unsafe {
        libc::umask(0o077);
    }
    let error = std::process::Command::new(bash)
        .arg(&owned.helper)
        .args([
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
        ])
        .stdin(Stdio::null())
        .exec();
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
        let leases = private_read(&directory.join("dnsmasq.leases"), 65536, 0)?.unwrap_or_default();
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
async fn run_helper(
    program: &str,
    args: &[&str],
    platform: &Platform,
    seconds: u64,
) -> Result<String> {
    let mut arguments = vec![program];
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
        Path::new("/bin/bash"),
        &arguments,
        Duration::from_secs(seconds),
        Some(&environment),
    )
    .await
}
async fn discard(mut pipe: impl AsyncRead + Unpin) -> Result<()> {
    let mut total = 0;
    let mut buf = [0; 8192];
    loop {
        let count = pipe
            .read(&mut buf)
            .await
            .map_err(|_| "Cannot drain helper output")?;
        if count == 0 {
            return Ok(());
        }
        total += count;
        if total > 1024 * 1024 {
            return Err("create_ap output exceeds limit");
        }
    }
}
struct Session {
    owned: Owned,
    child: Child,
    output: Vec<JoinHandle<Result<()>>>,
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
}
impl Backend {
    pub fn new(platform: Platform, program: &Path) -> Result<Arc<Self>> {
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
                if let Some(process) = &owned.process {
                    if private_read(&path.join("pid"), 32, 0o444)?.is_some_and(|b| {
                        String::from_utf8_lossy(&b).trim() == process.pid.to_string()
                    }) {
                        return Err("Owned helper state has no recovery snapshot");
                    }
                }
                continue;
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
                let allowed = owned.allowed(&self.platform.runtime);
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
    async fn recover_owned(&self, owned: &Owned) -> Result<()> {
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
        if !handle.verify(&session.owned.allowed(&self.platform.runtime))? {
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
        if relayforge_core::validate_reply(relayforge_core::Method::GetClients, &clients).is_err()
            || frequency != session.frequency
        {
            return Err("Invalid create_ap observation or channel changed");
        }
        let snapshot = self.platform.radio(&session.owned.parent).await?;
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
        session.status.client_count = clients.as_array().unwrap().len() as u32;
        Ok(clients)
    }
    pub async fn start(&self, config: &Config, mut cancelled: watch::Receiver<u64>) -> Value {
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
        let gate_cancelled = cancelled.clone();
        let prepare = async {
            let snapshot = self.platform.radio(&config["WIFI_IFACE"]).await?;
            let plan = snapshot.preflight(config)?;
            if config["SHARE_METHOD"] == "bridge" {
                return Err("Native create_ap declines bridge mode because it reconfigures the upstream interface");
            }
            if plan.switch_upstream || config.contains_key("COUNTRY") {
                return Err("Native create_ap preserves upstream and regulatory settings; reconnect explicitly before Start");
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
            let output = vec![
                tokio::spawn(discard(child.stdout.take().unwrap())),
                tokio::spawn(discard(child.stderr.take().unwrap())),
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
                json!({"success":false,"error":if cleanup.is_ok() {error} else {"create_ap startup failed; cleanup unconfirmed and journal retained"}})
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
