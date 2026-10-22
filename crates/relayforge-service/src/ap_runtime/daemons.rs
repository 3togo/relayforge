//! Private daemon rendering, durable exec gates and foreground child supervision.
mod leases;
use super::*;
use relayforge_core::config::{self, Config};
use std::{os::unix::process::CommandExt, process::Stdio};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
    task::JoinHandle,
};
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    version: u32,
    owner: Identity,
    marker: String,
    channel: String,
    band: String,
    mtu: Option<u32>,
    account: Option<leases::Account>,
    lease_identity: Option<(u64, u64)>,
    hashes: BTreeMap<String, String>,
    programs: BTreeMap<String, String>,
    runner: Option<Identity>,
    children: BTreeMap<String, Birth>,
    cleaned: bool,
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
impl Manifest {
    fn validate(&self, p: &Platform, data: &Snapshot) -> Result<()> {
        if self.version != 1
            || self.owner != data.owner
            || self.marker != data.fields["DAEMON_PIDFILE"]
            || !["2.4", "5"].contains(&self.band.as_str())
            || self
                .channel
                .parse::<u16>()
                .ok()
                .is_none_or(|v| !(1..=196).contains(&v))
            || self.mtu.is_some_and(|v| !(576..=65535).contains(&v))
            || (self.account.is_none() && self.lease_identity.is_some())
            || (self.account.is_some()
                && (self.runner.is_some() || !self.children.is_empty())
                && self.lease_identity.is_none())
            || self.children.len() > 2
            || self.programs.len() != if self.account.is_some() { 2 } else { 1 }
            || self.hashes.len() != self.programs.len()
        {
            return Err("Invalid daemon ownership manifest");
        }
        for name in ["hostapd", "dnsmasq"] {
            if name == "dnsmasq" && self.account.is_none() {
                continue;
            }
            let program = self.programs.get(name).ok_or("Missing daemon executable")?;
            let path = Path::new(program);
            let allowed = if let Some(root) = &p.fixture {
                path == root.join("bin").join(name)
            } else {
                ["/usr/sbin", "/usr/bin", "/sbin", "/bin"]
                    .iter()
                    .any(|v| path == Path::new(v).join(name))
            };
            if !allowed
                || !self
                    .hashes
                    .get(name)
                    .is_some_and(|v| v.len() == 64 && v.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err("Invalid daemon executable or configuration binding");
            }
        }
        for (name, birth) in &self.children {
            if !self.programs.contains_key(name) {
                return Err("Unknown daemon process");
            }
            birth.validate()?;
        }
        if let Some(runner) = &self.runner {
            runner.validate()?;
        }
        if let Some(account) = &self.account {
            account.validate(p)?;
        }
        Ok(())
    }
}
fn read(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<Option<(Manifest, Vec<u8>)>> {
    dir.read("daemons.json", true, LIMIT)?
        .map(|bytes| {
            let manifest: Manifest =
                serde_json::from_slice(&bytes).map_err(|_| "Invalid daemon manifest JSON")?;
            manifest.validate(p, data)?;
            Ok((manifest, bytes))
        })
        .transpose()
}
pub(super) fn frozen(p: &Platform, data: &Snapshot) -> Result<Config> {
    let marker = Path::new(&data.fields["DAEMON_PIDFILE"]);
    let root = Directory::open(
        marker
            .parent()
            .ok_or("Missing private configuration root")?,
    )?;
    let name = marker
        .with_extension("conf")
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let bytes = root
        .read(&name, true, LIMIT)?
        .ok_or("Missing frozen daemon configuration")?;
    let text = std::str::from_utf8(&bytes).map_err(|_| "Invalid frozen configuration encoding")?;
    let raw = config::parse_records(text)?;
    if !raw.contains_key("PASSPHRASE") {
        return Err("Frozen configuration must contain explicit credentials");
    }
    let config = config::validate(&raw)?;
    let directory = Path::new(&data.fields["CONFDIR"])
        .file_name()
        .unwrap()
        .to_string_lossy();
    if !directory.starts_with(&format!("create_ap.{}.conf.", config["WIFI_IFACE"]))
        || config["SHARE_METHOD"] == "bridge"
        || config.contains_key("COUNTRY")
        || config["GATEWAY"] != data.fields["GATEWAY"]
        || config["INTERNET_IFACE"] != data.fields["INTERNET_IFACE"]
        || config["NO_VIRT"] != data.fields["NO_VIRT"]
        || config["SHARE_METHOD"] != data.fields["SHARE_METHOD"]
        || (config["NO_DNS"] == "1" || config["NO_DNSMASQ"] == "1")
            != (data.fields["NO_DNS"] == "1")
    {
        return Err("Daemon configuration differs from the owned snapshot");
    }
    let _ = p;
    Ok(config)
}
fn render(
    config: &Config,
    data: &Snapshot,
    manifest: &Manifest,
) -> Result<BTreeMap<String, String>> {
    if config["FREQ_BAND"] != manifest.band || config["CHANNEL"] != manifest.channel {
        return Err("Daemon radio plan changed");
    }
    let path = &data.fields["CONFDIR"];
    let iface = &data.fields["WIFI_IFACE"];
    let mut host=format!("beacon_int=100\nssid2={}\ninterface={iface}\ndriver=nl80211\nchannel={}\nctrl_interface={path}/hostapd_ctrl\nctrl_interface_group=0\nignore_broadcast_ssid={}\nap_isolate={}\nhw_mode={}\n",
        config["SSID"].as_bytes().iter().map(|v|format!("{v:02x}")).collect::<String>(),manifest.channel,config["HIDDEN"],config["ISOLATE_CLIENTS"],if manifest.band=="2.4"{"g"}else{"a"});
    if config["MAC_FILTER"] == "1" {
        host.push_str(&format!(
            "macaddr_acl=1\naccept_mac_file={}\n",
            config["MAC_FILTER_ACCEPT"]
        ));
    }
    if config["IEEE80211N"] == "1" {
        host.push_str("ieee80211n=1\nht_capab=[HT40+]\n");
    }
    if config["IEEE80211AC"] == "1" {
        host.push_str("ieee80211ac=1\n");
    }
    if config["IEEE80211AX"] == "1" {
        host.push_str("ieee80211ax=1\n");
    }
    if config["IEEE80211N"] == "1" || config["IEEE80211AC"] == "1" {
        host.push_str("wmm_enabled=1\n");
    }
    let wpa = if config["WPA_VERSION"] == "3" {
        "2"
    } else {
        &config["WPA_VERSION"]
    };
    host.push_str(&format!(
        "wpa={wpa}\nwpa_{}={}\nwpa_key_mgmt={}\nwpa_pairwise=CCMP\nrsn_pairwise=CCMP\n",
        if config["USE_PSK"] == "1" {
            "psk"
        } else {
            "passphrase"
        },
        config["PASSPHRASE"],
        if config["WPA_VERSION"] == "3" {
            "WPA-PSK SAE"
        } else {
            "WPA-PSK"
        }
    ));
    if config["WPA_VERSION"] == "3" {
        host.push_str("ieee80211w=1\n");
    }
    let mut files = BTreeMap::from([("hostapd".into(), host)]);
    if manifest.account.is_some() {
        let gateway = &config["GATEWAY"];
        let subnet = gateway.rsplit_once('.').ok_or("Invalid gateway")?.0;
        let mut dns=format!("interface={iface}\nlisten-address={gateway}\nbind-dynamic\ndhcp-range={subnet}.1,{subnet}.254,255.255.255.0,24h\ndhcp-option-force=option:router,{gateway}\ndhcp-option-force=option:dns-server,{}\n",if config["DHCP_DNS"]=="gateway"{gateway}else{&config["DHCP_DNS"]});
        if let Some(mtu) = manifest.mtu {
            dns.push_str(&format!("dhcp-option-force=option:mtu,{mtu}\n"));
        }
        if config["ETC_HOSTS"] == "0" {
            dns.push_str("no-hosts\n");
        }
        files.insert("dnsmasq".into(), dns);
    }
    Ok(files)
}
fn program(p: &Platform, name: &str) -> Result<String> {
    let path = if let Some(root) = &p.fixture {
        root.join("bin").join(name)
    } else {
        ["/usr/sbin", "/usr/bin", "/sbin", "/bin"]
            .iter()
            .map(|v| Path::new(v).join(name))
            .find(|v| v.is_file())
            .ok_or("Required AP daemon is unavailable")?
            .canonicalize()
            .map_err(|_| "Cannot resolve daemon executable")?
    };
    crate::ap_process::trusted_file(&path)
}
fn write_config(
    dir: &Directory,
    name: &str,
    bytes: &[u8],
    previous: Option<&Manifest>,
) -> Result<()> {
    let filename = format!("{name}.conf");
    let old = dir.read(&filename, true, LIMIT)?;
    if let Some(old) = &old {
        if previous.and_then(|v| v.hashes.get(name)) != Some(&digest(old)) {
            return Err("Daemon configuration changed externally");
        }
    }
    dir.write_bytes(&filename, bytes, old.as_deref())
}
pub(super) async fn configure(p: &Platform, dir: &Directory, values: &[String]) -> Result<String> {
    let _lock = dir.transaction_lock(c".daemons.lock").await?;
    let (mut data, snapshot_bytes) = snapshot(p, dir)?;
    data.interfaces(p)?;
    if data.owner.current()?.is_none() {
        return Err("Daemon owner exited");
    }
    let config = frozen(p, &data)?;
    radio_plan::check(p, dir, false).await?;
    if values[0] != config["CHANNEL"] || values[1] != config["FREQ_BAND"] || values[2] != "nl80211"
    {
        return Err("Unsupported daemon radio or driver plan");
    }
    let previous = read(p, dir, &data)?;
    if previous
        .as_ref()
        .is_some_and(|(v, _)| v.runner.is_some() || !v.children.is_empty() || v.cleaned)
    {
        return Err("Stop must finish the prior daemon session");
    }
    let account = if config["NO_DNSMASQ"] == "0" {
        Some(leases::account(p)?)
    } else {
        None
    };
    let mtu = match fs::read_to_string(p.net.join(&data.fields["INTERNET_IFACE"]).join("mtu")) {
        Ok(v) => Some(
            v.trim()
                .parse::<u32>()
                .ok()
                .filter(|v| (576..=65535).contains(v))
                .ok_or("Invalid upstream MTU")?,
        ),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(_) => return Err("Cannot inspect upstream MTU"),
    };
    let mut manifest = Manifest {
        version: 1,
        owner: data.owner.clone(),
        marker: data.fields["DAEMON_PIDFILE"].clone(),
        channel: values[0].clone(),
        band: values[1].clone(),
        mtu,
        account,
        lease_identity: previous.as_ref().and_then(|v| v.0.lease_identity),
        hashes: BTreeMap::new(),
        programs: BTreeMap::new(),
        runner: None,
        children: BTreeMap::new(),
        cleaned: false,
    };
    let files = render(&config, &data, &manifest)?;
    for (name, text) in &files {
        manifest
            .hashes
            .insert(name.clone(), digest(text.as_bytes()));
        manifest.programs.insert(name.clone(), program(p, name)?);
    }
    manifest.validate(p, &data)?;
    if let Some((old, _)) = &previous {
        if old.hashes != manifest.hashes
            || old.programs != manifest.programs
            || old.account != manifest.account
        {
            return Err("Stop before changing daemon configuration");
        }
    }
    dir.write(
        "daemons.json",
        &manifest,
        previous.as_ref().map(|v| v.1.as_slice()),
    )?;
    for (name, text) in files {
        write_config(dir, &name, text.as_bytes(), previous.as_ref().map(|v| &v.0))?;
    }
    if let Some(account) = &manifest.account {
        manifest.lease_identity = Some(leases::ensure(
            p,
            &manifest.marker,
            account,
            manifest.lease_identity,
        )?);
        let bytes = dir
            .read("daemons.json", true, LIMIT)?
            .ok_or("Missing daemon lease intent")?;
        dir.write("daemons.json", &manifest, Some(&bytes))?;
    }
    let port = if data.fields["NO_DNS"] == "1" {
        "0"
    } else {
        "5353"
    };
    data.fields.insert("DNS_PORT".into(), port.into());
    dir.write("recovery.json", &data, Some(&snapshot_bytes))?;
    Ok(format!("DNS_PORT={}", quote(port)))
}
fn arguments(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
    m: &Manifest,
    name: &str,
) -> Result<Vec<String>> {
    match name {
        "hostapd" => Ok(vec![dir
            .path
            .join("hostapd.conf")
            .to_string_lossy()
            .into_owned()]),
        "dnsmasq" => {
            let a = m.account.as_ref().ok_or("DNS daemon is disabled")?;
            Ok(vec![
                "--keep-in-foreground".into(),
                format!("--conf-file={}/dnsmasq.conf", dir.path.display()),
                "--pid-file=".into(),
                format!(
                    "--dhcp-leasefile={}",
                    leases::path(p, &m.marker)?.join("dnsmasq.leases").display()
                ),
                format!("--port={}", data.fields["DNS_PORT"]),
                format!("--user={}", a.user),
                format!("--group={}", a.group),
            ])
        }
        _ => Err("Unknown AP daemon"),
    }
}
fn gate_arguments(p: &Platform, dir: &Directory, name: &str) -> Vec<String> {
    let mut args = if let Some(root) = &p.fixture {
        vec![
            "--test-ap-runtime".into(),
            root.to_string_lossy().into_owned(),
        ]
    } else {
        vec!["--create-ap-runtime".into()]
    };
    args.extend([
        "daemon-exec".into(),
        dir.path.to_string_lossy().into_owned(),
        name.into(),
    ]);
    args
}
async fn drain(mut stream: impl AsyncRead + Unpin) -> Result<()> {
    let mut size = 0;
    let mut buffer = [0u8; 8192];
    loop {
        let n = stream
            .read(&mut buffer)
            .await
            .map_err(|_| "Cannot drain AP daemon output")?;
        if n == 0 {
            return Ok(());
        }
        size += n;
        if size > 1024 * 1024 {
            return Err("AP daemon output exceeds budget");
        }
    }
}
struct Running {
    child: Child,
    output: Vec<JoinHandle<Result<()>>>,
}
impl Drop for Running {
    fn drop(&mut self) {
        for task in &self.output {
            task.abort();
        }
    }
}
async fn launch(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
    m: &mut Manifest,
    name: &str,
) -> Result<Running> {
    if data.owner.current()?.is_none() {
        return Err("Daemon owner exited before launch");
    }
    network::daemon_permit(p, dir).await?;
    let exe = std::env::current_exe().map_err(|_| "Cannot find daemon execution gate")?;
    let mut command = Command::new(&exe);
    command
        .args(gate_arguments(p, dir, name))
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let child = command
        .spawn()
        .map_err(|_| "Cannot spawn gated AP daemon")?;
    let mut running = Running {
        child,
        output: Vec::new(),
    };
    let birth = Birth::capture(running.child.id().ok_or("Missing daemon PID")?)?;
    let bytes = dir
        .read("daemons.json", true, LIMIT)?
        .ok_or("Missing daemon manifest")?;
    m.children.insert(name.into(), birth.clone());
    dir.write("daemons.json", m, Some(&bytes))?;
    let filename = format!("{name}.pid");
    let prior = dir.read(&filename, true, 32)?;
    if let Some(v) = &prior {
        if std::str::from_utf8(v)
            .ok()
            .and_then(|v| v.trim().parse::<u32>().ok())
            .is_some_and(|v| observe(v).ok().flatten().is_some())
        {
            return Err("Prior daemon PID is still live");
        }
    }
    dir.write_bytes(
        &filename,
        format!("{}\n", birth.pid).as_bytes(),
        prior.as_deref(),
    )?;
    running
        .output
        .push(tokio::spawn(drain(running.child.stdout.take().unwrap())));
    running
        .output
        .push(tokio::spawn(drain(running.child.stderr.take().unwrap())));
    running
        .child
        .stdin
        .take()
        .ok_or("Missing daemon execution gate")?
        .write_all(&[1])
        .await
        .map_err(|_| "Cannot release daemon execution gate")?;
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Ok(Some(observed)) = observe(birth.pid) {
            let expected = std::iter::once(m.programs[name].clone())
                .chain(arguments(p, dir, data, m, name)?)
                .collect::<Vec<_>>();
            let allowed_uid = observed.uid == unsafe { libc::geteuid() }
                || (name == "dnsmasq" && m.account.as_ref().is_some_and(|v| v.uid == observed.uid));
            if allowed_uid && observed.args == expected && observed.identity.exe == m.programs[name]
            {
                let filename = format!("{name}.identity");
                let old = dir.read(&filename, true, LIMIT)?;
                dir.write(&filename, &observed.identity, old.as_deref())?;
                break;
            }
        }
        if running
            .child
            .try_wait()
            .map_err(|_| "Cannot inspect gated daemon")?
            .is_some()
            || Instant::now() >= deadline
        {
            return Err("AP daemon failed to execute");
        }
        sleep(Duration::from_millis(10)).await;
    }
    Ok(running)
}
pub(super) async fn exec(p: &Platform, dir: &Directory, name: &str) -> Result<()> {
    let mut ack = [0u8; 1];
    let mut poll = libc::pollfd {
        fd: 0,
        events: libc::POLLIN,
        revents: 0,
    };
    if unsafe { libc::poll(&mut poll, 1, 3000) } <= 0 {
        return Err("Daemon gate acknowledgement timed out");
    }
    std::io::stdin()
        .read_exact(&mut ack)
        .map_err(|_| "Daemon gate was not acknowledged")?;
    if ack != [1] {
        return Err("Invalid daemon gate acknowledgement");
    }
    let (data, _) = snapshot(p, dir)?;
    if data.owner.current()?.is_none() {
        return Err("Daemon owner exited before execution");
    }
    let (m, _) = read(p, dir, &data)?.ok_or("Missing daemon execution intent")?;
    let birth = m.children.get(name).ok_or("Unrecorded daemon gate")?;
    if birth.pid != std::process::id() || !birth.live()? || m.cleaned {
        return Err("Daemon execution gate identity changed");
    }
    verify_files(dir, &m)?;
    let program = crate::ap_process::trusted_file(Path::new(&m.programs[name]))?;
    if program != m.programs[name] {
        return Err("Daemon installation changed");
    }
    unsafe {
        libc::umask(0o077);
    }
    let error = std::process::Command::new(program)
        .args(arguments(p, dir, &data, &m, name)?)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .exec();
    let _ = error;
    Err("Cannot execute AP daemon")
}
fn verify_files(dir: &Directory, m: &Manifest) -> Result<()> {
    for (name, hash) in &m.hashes {
        let bytes = dir
            .read(&format!("{name}.conf"), true, LIMIT)?
            .ok_or("Missing daemon configuration")?;
        if digest(&bytes) != *hash {
            return Err("Daemon configuration changed before launch");
        }
    }
    Ok(())
}
fn enabled_status(text: &str) -> bool {
    text.lines()
        .filter(|v| v.starts_with("state="))
        .collect::<Vec<_>>()
        == ["state=ENABLED"]
}
async fn enabled(p: &Platform, dir: &Directory, data: &Snapshot) -> Result<bool> {
    if let Some(root) = &p.fixture {
        let dir_root = Directory::open(root)?;
        let value = dir_root
            .read("daemon-controls.json", true, LIMIT)?
            .unwrap_or_else(|| b"{}".to_vec());
        let value: serde_json::Value =
            serde_json::from_slice(&value).map_err(|_| "Invalid daemon fixture controls")?;
        return Ok(value.get("ready").and_then(|v| v.as_bool()).unwrap_or(true)
            && dir.read("hostapd.conf.ready", true, 16)?.is_some());
    }
    let executable = program(p, "hostapd_cli")?;
    let control = dir.path.join("hostapd_ctrl");
    let args = [
        "-p",
        control.to_str().ok_or("Invalid control socket path")?,
        "-i",
        &data.fields["WIFI_IFACE"],
        "status",
    ];
    match radio::command(Path::new(&executable), &args, Duration::from_secs(1)).await {
        Ok(text) => Ok(enabled_status(&text)),
        Err(_) => Ok(false),
    }
}
pub(super) async fn run(p: &Platform, dir: &Directory) -> Result<()> {
    let _lock = dir.transaction_lock(c".daemons.lock").await?;
    let (data, _) = snapshot(p, dir)?;
    let (mut m, bytes) = read(p, dir, &data)?.ok_or("Configure AP daemons before launch")?;
    if m.runner.is_some() || !m.children.is_empty() || m.cleaned {
        return Err("Stop must clear the prior daemon session");
    }
    let config = frozen(p, &data)?;
    radio_plan::check(p, dir, false).await?;
    let rendered = render(&config, &data, &m)?;
    for (name, text) in rendered {
        if m.hashes.get(&name) != Some(&digest(text.as_bytes())) {
            return Err("Frozen daemon configuration changed");
        }
    }
    verify_files(dir, &m)?;
    network::daemon_permit(p, dir).await?;
    let identity = observe(std::process::id())?
        .ok_or("Cannot observe daemon supervisor")?
        .identity;
    m.runner = Some(identity);
    dir.write("daemons.json", &m, Some(&bytes))?;
    let mut children = Vec::new();
    if m.account.is_some() {
        children.push(launch(p, dir, &data, &mut m, "dnsmasq").await?);
    }
    children.push(launch(p, dir, &data, &mut m, "hostapd").await?);
    let deadline = Instant::now() + Duration::from_secs(if p.fixture.is_some() { 1 } else { 8 });
    let mut ready = false;
    loop {
        if data.owner.current()?.is_none() {
            return Err("AP owner exited");
        }
        data.interfaces(p)?;
        for child in &mut children {
            if child
                .child
                .try_wait()
                .map_err(|_| "Cannot inspect AP daemon")?
                .is_some()
            {
                return Err("AP daemon exited");
            }
            let mut index = 0;
            while index < child.output.len() {
                if child.output[index].is_finished() {
                    if !matches!(child.output.swap_remove(index).await, Ok(Ok(()))) {
                        return Err("AP daemon output budget exceeded");
                    }
                } else {
                    index += 1;
                }
            }
        }
        if !ready {
            if enabled(p, dir, &data).await? {
                if data.owner.current()?.is_none() {
                    return Err("AP owner exited before readiness");
                }
                data.interfaces(p)?;
                let marker = Path::new(&m.marker);
                let root = Directory::open(marker.parent().unwrap())?;
                let filename = marker.file_name().unwrap().to_str().unwrap();
                let pid = root
                    .read(filename, true, 32)?
                    .ok_or("Missing owner pidfile")?;
                if std::str::from_utf8(&pid).ok().map(str::trim)
                    != Some(&data.owner.pid.to_string())
                {
                    return Err("AP owner pidfile changed");
                }
                let ready_name = format!("{filename}.ready");
                let old = root.read(&ready_name, true, 32)?;
                if old.is_some() {
                    return Err("Unexpected prior daemon readiness marker");
                }
                root.write_bytes(
                    &ready_name,
                    format!("{}\n", data.owner.pid).as_bytes(),
                    None,
                )?;
                ready = true;
            } else if Instant::now() >= deadline {
                return Err("Hostapd readiness deadline exceeded");
            }
        }
        sleep(Duration::from_millis(50)).await;
    }
}
async fn stop_child(
    p: &Platform,
    dir: &Directory,
    data: &Snapshot,
    m: &Manifest,
    name: &str,
    birth: &Birth,
) -> Result<()> {
    let Some(handle) = Handle::open(birth)? else {
        return Ok(());
    };
    let runner = m
        .runner
        .as_ref()
        .ok_or("Missing daemon supervisor identity")?;
    let gate = std::iter::once(runner.exe.clone())
        .chain(gate_arguments(p, dir, name))
        .collect::<Vec<_>>();
    let argv = std::iter::once(m.programs[name].clone())
        .chain(arguments(p, dir, data, m, name)?)
        .collect::<Vec<_>>();
    let allowed = [(runner.exe.clone(), gate), (m.programs[name].clone(), argv)];
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        let Some(current) = observe(birth.pid)? else {
            return Ok(());
        };
        if current.uid != unsafe { libc::geteuid() }
            && !(name == "dnsmasq" && m.account.as_ref().is_some_and(|v| v.uid == current.uid))
        {
            return Err("Daemon privilege identity changed");
        }
        handle.signal_as(signal, &allowed, current.uid)?;
        let deadline = Instant::now() + Duration::from_secs(2);
        while !handle.exited()? && Instant::now() < deadline {
            sleep(Duration::from_millis(20)).await;
        }
        if handle.exited()? {
            return Ok(());
        }
    }
    Err("Owned AP daemon survived cleanup")
}
pub(super) async fn stop_all(p: &Platform, dir: &Directory) -> Result<()> {
    if dir.read("daemons.json", true, LIMIT)?.is_none() {
        return Ok(());
    }
    let (data, _) = snapshot(p, dir)?;
    let Some((mut m, _)) = read(p, dir, &data)? else {
        return Ok(());
    };
    let _stop = dir.transaction_lock(c".daemon-stop.lock").await?;
    if let Some(runner) = &m.runner {
        if runner.pid == std::process::id() {
            return Err("Daemon supervisor cannot recover itself");
        }
        if let Some(actual) = observe(runner.pid)? {
            if actual.identity.starttime == runner.starttime && actual.identity != *runner {
                return Err("Daemon supervisor identity changed");
            }
        }
        stop(runner).await?;
    }
    let _start = dir.transaction_lock(c".daemons.lock").await?;
    // Re-read after the supervisor has stopped: it may have recorded another gated child.
    let (latest, bytes) = read(p, dir, &data)?.ok_or("Daemon manifest disappeared")?;
    m = latest;
    let mut failure = None;
    for (name, birth) in &m.children {
        if let Err(error) = stop_child(p, dir, &data, &m, name, birth).await {
            failure.get_or_insert(error);
        }
    }
    if let Some(error) = failure {
        return Err(error);
    }
    if let Some(account) = &m.account {
        if let Some(id) = m.lease_identity {
            match fs::symlink_metadata(leases::path(p, &m.marker)?) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => return Err("Cannot confirm lease cleanup"),
                Ok(_) => {
                    leases::verify(p, &m.marker, account, id)?;
                    leases::cleanup(p, &m.marker, account)?;
                }
            }
        } else if fs::symlink_metadata(leases::path(p, &m.marker)?).is_ok() {
            return Err("Unconfirmed lease directory ownership");
        }
    }
    m.cleaned = true;
    dir.write("daemons.json", &m, Some(&bytes))
}
pub(super) fn check(p: &Platform, dir: &Directory) -> Result<()> {
    let (data, _) = snapshot(p, dir)?;
    if let Some((m, _)) = read(p, dir, &data)? {
        if !m.cleaned {
            return Err("Daemon cleanup is unconfirmed");
        }
        if let Some(runner) = &m.runner {
            if observe(runner.pid)?.is_some_and(|v| v.identity.starttime == runner.starttime) {
                return Err("Daemon supervisor survived cleanup");
            }
        }
        for birth in m.children.values() {
            if birth.live()? {
                return Err("Owned daemon survived cleanup");
            }
        }
        if leases::path(p, &m.marker)?.exists() {
            return Err("Owned lease directory survived cleanup");
        }
    }
    Ok(())
}
pub(super) fn read_leases(p: &Platform, dir: &Directory) -> Result<Vec<u8>> {
    let (data, _) = snapshot(p, dir)?;
    if let Some((m, _)) = read(p, dir, &data)? {
        if let Some(account) = m.account {
            let id = m
                .lease_identity
                .ok_or("Unconfirmed lease directory ownership")?;
            leases::verify(p, &m.marker, &account, id)?;
            return leases::read(p, &m.marker, &account);
        }
        return Ok(Vec::new());
    }
    Ok(dir
        .read("dnsmasq.leases", false, LIMIT)?
        .unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::enabled_status;
    #[test]
    fn readiness_requires_one_exact_enabled_state() {
        assert!(enabled_status("state=ENABLED\nphy=phy0\nfreq=2412\n"));
        for status in [
            "",
            "state=DISABLED\n",
            "foo=ENABLED\n",
            "state=ENABLED \n",
            "state=ENABLED\nstate=DISABLED\n",
            "state=ENABLED\0",
        ] {
            assert!(!enabled_status(status));
        }
    }
}
