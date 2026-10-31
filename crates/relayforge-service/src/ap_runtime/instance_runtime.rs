//! Native helper options, atomic instance publication, signals and owned housekeeping.
pub(super) mod completion;
use super::*;
use relayforge_core::config::{self, Config};
use std::os::unix::fs::DirBuilderExt;
use tokio::{
    process::Command,
    signal::unix::{signal, SignalKind},
};

#[derive(Debug, PartialEq)]
enum Options {
    Recover(PathBuf),
    Start {
        config: PathBuf,
        marker: PathBuf,
        band: String,
        channel: String,
        bridge: bool,
    },
}
fn parse(args: &[String]) -> Result<Options> {
    if args.iter().map(String::len).sum::<usize>() > LIMIT {
        return Err("Instance options exceed limit");
    }
    if let [mode, path] = args {
        if mode == "--recover" {
            return Ok(Options::Recover(path.into()));
        }
    }
    let mut values = BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let name = args[index].as_str();
        let value = match name {
            "--config" | "--pidfile" | "--freq-band" | "-c" => {
                index += 1;
                args.get(index)
                    .ok_or("Missing native instance option value")?
                    .clone()
            }
            "--daemon" | "--no-haveged" | "--allow-existing-bridge" => String::new(),
            _ => return Err("Unsupported native instance option"),
        };
        if values.insert(name, value).is_some() {
            return Err("Repeated native instance option");
        }
        index += 1;
    }
    let bridge = values.remove("--allow-existing-bridge").is_some();
    if values.len() != 6 || !values.contains_key("--daemon") || !values.contains_key("--no-haveged")
    {
        return Err("Incomplete native instance options");
    }
    let band = values.remove("--freq-band").ok_or("Missing native band")?;
    let channel = values.remove("-c").ok_or("Missing native channel")?;
    if !["2.4", "5"].contains(&band.as_str())
        || channel
            .parse::<u16>()
            .ok()
            .is_none_or(|v| !(1..=196).contains(&v) || v.to_string() != channel)
    {
        return Err("Invalid native instance radio plan");
    }
    Ok(Options::Start {
        bridge,
        config: values
            .remove("--config")
            .ok_or("Missing native config")?
            .into(),
        marker: values
            .remove("--pidfile")
            .ok_or("Missing native pidfile")?
            .into(),
        band,
        channel,
    })
}
fn private_mkdir(path: &Path) -> Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err("Cannot create private instance runtime"),
    }
    directory(path, true)
}
fn options(
    p: &Platform,
    path: &Path,
    marker: &Path,
    band: &str,
    channel: &str,
    bridge: bool,
) -> Result<(Config, Directory, String)> {
    let root = p.fixture.as_deref().unwrap_or(Path::new("/run/relayforge"));
    directory(root, true)?;
    let token = marker
        .file_name()
        .and_then(|v| v.to_str())
        .and_then(|v| v.strip_prefix("create-ap-"))
        .and_then(|v| v.strip_suffix(".pid"))
        .filter(|v| v.len() == 32 && v.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or("Invalid instance pidfile marker")?;
    if marker.parent() != Some(root) || path != marker.with_extension("conf") {
        return Err("Instance configuration is outside the private runtime");
    }
    let dir = Directory::open(root)?;
    let bytes = dir
        .read(
            path.file_name()
                .unwrap()
                .to_str()
                .ok_or("Invalid config filename")?,
            true,
            LIMIT,
        )?
        .ok_or("Missing private instance configuration")?;
    let raw = config::parse_records(
        std::str::from_utf8(&bytes).map_err(|_| "Invalid instance configuration encoding")?,
    )?;
    if !raw.contains_key("PASSPHRASE") {
        return Err("Explicit instance credentials are required");
    }
    let config = config::validate(&raw)?;
    relayforge_core::ap_policy::security(&config)?;
    if config["BACKEND"] != "create_ap"
        || (config["SHARE_METHOD"] == "bridge") != bridge
        || config["CHANNEL"] != channel
        || config["FREQ_BAND"] != band
    {
        return Err("Unsupported or changed frozen instance plan");
    }
    if bridge {
        network::bridge::validate_config(&config)?;
    }
    for name in [
        marker.file_name().unwrap().to_str().unwrap().to_string(),
        format!("{}.ready", marker.file_name().unwrap().to_str().unwrap()),
    ] {
        if dir.read(&name, true, 32)?.is_some() {
            return Err("Instance marker already exists; cleanup is required");
        }
    }
    Ok((config, dir, token.into()))
}
async fn publish(p: &Platform, config: &Config, marker: &Path, token: &str) -> Result<PathBuf> {
    private_mkdir(p.root.parent().ok_or("Missing engine parent")?)?;
    private_mkdir(&p.root)?;
    let engine = Directory::open(&p.root)?;
    let name = format!("create_ap.{}.conf.{}", config["WIFI_IFACE"], &token[..8]);
    let destination = p.root.join(&name);
    if engine
        .read(&format!(".cleanup-{name}.json"), true, LIMIT)?
        .is_some()
    {
        return Err("Complete prior instance retirement before allocating again");
    }
    let temporary = p.root.join(format!(".pending-{token}"));
    fs::DirBuilder::new()
        .mode(0o700)
        .create(&temporary)
        .map_err(|_| "Instance allocation already exists or is unavailable")?;
    let dir = Directory::open(&temporary)?;
    let result = async {
        let mut fields: BTreeMap<String, String> = FIELDS
            .iter()
            .map(|v| (v.to_string(), String::new()))
            .collect();
        for (key, value) in [
            (
                "CONFDIR",
                destination.to_str().ok_or("Invalid instance path")?,
            ),
            (
                "COMMON_CONFDIR",
                p.root
                    .join("create_ap.common.conf")
                    .to_str()
                    .ok_or("Invalid common path")?,
            ),
            ("WIFI_IFACE", &config["WIFI_IFACE"]),
            ("INTERNET_IFACE", &config["INTERNET_IFACE"]),
            ("NO_VIRT", &config["NO_VIRT"]),
            ("SHARE_METHOD", &config["SHARE_METHOD"]),
            (
                "BRIDGE_IFACE",
                if config["SHARE_METHOD"] == "bridge" {
                    &config["INTERNET_IFACE"]
                } else {
                    ""
                },
            ),
            (
                "NO_DNS",
                if config["SHARE_METHOD"] == "bridge"
                    || config["NO_DNS"] == "1"
                    || config["NO_DNSMASQ"] == "1"
                {
                    "1"
                } else {
                    "0"
                },
            ),
            ("GATEWAY", &config["GATEWAY"]),
            ("RUNNING_AS_DAEMON", "1"),
            ("BRIDGE_CREATED", "0"),
            ("PHYSICAL_CONFIGURED", "0"),
            (
                "DAEMON_PIDFILE",
                marker.to_str().ok_or("Invalid marker path")?,
            ),
            ("RULE_COMMENT", &format!("wifi-relay-{name}")),
        ] {
            fields.insert(key.into(), value.into());
        }
        let identity = dir
            .fd
            .metadata()
            .map_err(|_| "Cannot bind newly allocated instance directory")?;
        let mut data = Snapshot {
            directory: Some(DirectoryIdentity {
                device: identity.dev(),
                inode: identity.ino(),
            }),
            owner: observe(std::process::id())?
                .ok_or("Missing instance owner")?
                .identity,
            fields,
            arrays: ARRAYS.iter().map(|k| (k.to_string(), Vec::new())).collect(),
            interfaces: BTreeMap::new(),
        };
        for iface in [&config["WIFI_IFACE"], &config["INTERNET_IFACE"]] {
            if let Some(id) = p.interface(iface)? {
                data.interfaces.insert(iface.clone(), id);
            }
        }
        data.validate(p, &destination)?;
        dir.write("recovery.json", &data, None)?;
        if config["SHARE_METHOD"] == "bridge" {
            network::bridge::prepare(p, &dir, &data, config).await?;
        }
        dir.write_bytes("pid", format!("{}\n", data.owner.pid).as_bytes(), None)?;
        let source = filename(temporary.file_name().unwrap().to_str().unwrap())?;
        let target = filename(&name)?;
        if unsafe {
            libc::renameat2(
                engine.fd.as_raw_fd(),
                source.as_ptr(),
                engine.fd.as_raw_fd(),
                target.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        } != 0
        {
            return Err("Cannot publish instance without replacing existing state");
        }
        engine
            .fd
            .sync_all()
            .map_err(|_| "Cannot sync published instance")?;
        Ok(destination)
    }
    .await;
    if result.is_err() && temporary.is_dir() {
        // Only the newly allocated unpublished records can be discarded. No recursion.
        for name in ["pid", "recovery.json", "bridge.json"] {
            let _ = fs::remove_file(temporary.join(name));
        }
        let _ = fs::remove_dir(&temporary);
    }
    result
}
async fn start(
    p: &Platform,
    path: &Path,
    marker_root: &Directory,
    marker: &Path,
    band: &str,
    channel: &str,
) -> Result<()> {
    for operation in ["radio-check", "interface-create", "device-release"] {
        action(p, operation, path, &[]).await?;
    }
    let dir = p.open(path, false)?;
    let (data, _) = snapshot(p, &dir)?;
    dir.write_bytes(
        "wifi_iface",
        format!("{}\n", data.fields["WIFI_IFACE"]).as_bytes(),
        None,
    )?;
    action(
        p,
        "daemons-configure",
        path,
        &[channel.into(), band.into(), "nl80211".into()],
    )
    .await?;
    action(p, "interface-configure", path, &[]).await?;
    let (mut data, bytes) = snapshot(p, &dir)?;
    if data.fields["NO_VIRT"] == "1" {
        data.fields.insert("PHYSICAL_CONFIGURED".into(), "1".into());
        dir.write("recovery.json", &data, Some(&bytes))?;
    }
    if data.fields["SHARE_METHOD"] == "nat" {
        action(p, "forwarding-enable", path, &[]).await?;
        action(p, "firewall-add", path, &["nat".into()]).await?;
    }
    if data.fields["NO_DNS"] == "0" {
        action(p, "firewall-add", path, &["dns".into()]).await?;
    }
    let config = daemons::frozen(p, &data)?;
    if config["SHARE_METHOD"] != "bridge" && config["NO_DNSMASQ"] == "0" {
        action(p, "firewall-add", path, &["dhcp".into()]).await?;
    }
    marker_root.write_bytes(
        marker.file_name().unwrap().to_str().unwrap(),
        format!("{}\n", data.owner.pid).as_bytes(),
        None,
    )?;
    let exe = std::env::current_exe().map_err(|_| "Cannot locate daemon runner")?;
    let mut command = Command::new(exe);
    if let Some(root) = &p.fixture {
        command.arg("--test-ap-runtime").arg(root);
    } else {
        command.arg("--create-ap-runtime");
    }
    let status = command
        .args(["daemons-run", path.to_str().ok_or("Invalid daemon path")?])
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| "Cannot start native daemon runner")?
        .wait()
        .await
        .map_err(|_| "Cannot wait for native daemon runner")?;
    if status.success() {
        Ok(())
    } else {
        Err("Native daemon runner failed")
    }
}
fn known(name: &str, fixture: bool) -> bool {
    [
        "recovery.json",
        "radio.json",
        "device.json",
        "interface.json",
        "bridge.json",
        "daemons.json",
        "pid",
        "wifi_iface",
        "nat_internet_iface",
        ".instance.lock",
        ".radio.lock",
        ".network.lock",
        ".firewall.lock",
        ".daemons.lock",
        ".daemon-stop.lock",
    ]
    .contains(&name)
        || CHILDREN
            .iter()
            .any(|(stem, _)| name == format!("{stem}.pid") || name == format!("{stem}.identity"))
        || ["hostapd.conf", "dnsmasq.conf"].contains(&name)
        || fixture && ["hostapd.conf.ready", "dnsmasq.conf.ready"].contains(&name)
}
fn current_directory(dir: &Directory) -> Result<()> {
    directory(&dir.path, false)?;
    let current = fs::symlink_metadata(&dir.path).map_err(|_| "Instance directory disappeared")?;
    let held = dir
        .fd
        .metadata()
        .map_err(|_| "Cannot inspect retained instance directory")?;
    if current.dev() != held.dev() || current.ino() != held.ino() {
        return Err("Instance directory was replaced; retaining evidence");
    }
    Ok(())
}
fn file_proof(dir: &Directory, name: &str) -> Result<(u64, u64, String)> {
    let filename = filename(name)?;
    let metadata = || -> Result<libc::stat> {
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                dir.fd.as_raw_fd(),
                filename.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } != 0
        {
            return Err("Cannot inspect instance file identity");
        }
        Ok(unsafe { stat.assume_init() })
    };
    let before = metadata()?;
    let bytes = dir
        .read(name, false, LIMIT)?
        .ok_or("Instance file disappeared during cleanup")?;
    let after = metadata()?;
    if before.st_dev != after.st_dev
        || before.st_ino != after.st_ino
        || before.st_mode != after.st_mode
        || before.st_uid != after.st_uid
        || before.st_nlink != after.st_nlink
        || before.st_size != after.st_size
    {
        return Err("Instance file changed during inspection");
    }
    Ok((
        after.st_dev,
        after.st_ino,
        format!("{:x}", Sha256::digest(bytes)),
    ))
}
type Inventory = (BTreeMap<String, (u64, u64, String)>, Option<(u64, u64)>);
fn inventory(dir: &Directory, fixture: bool) -> Result<Inventory> {
    current_directory(dir)?;
    let mut files = BTreeMap::new();
    let mut control = None;
    for entry in fs::read_dir(&dir.path).map_err(|_| "Cannot list completed instance")? {
        let entry = entry.map_err(|_| "Cannot inspect completed instance")?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "Invalid instance filename")?;
        if name == "hostapd_ctrl" {
            let meta = fs::symlink_metadata(entry.path())
                .map_err(|_| "Cannot inspect hostapd control directory")?;
            if !meta.is_dir()
                || meta.uid() != unsafe { libc::geteuid() }
                || meta.mode() & 0o002 != 0
                || fs::read_dir(entry.path())
                    .map_err(|_| "Cannot inspect hostapd control contents")?
                    .next()
                    .is_some()
            {
                return Err("Unsafe or occupied hostapd control directory retained");
            }
            control = Some((meta.dev(), meta.ino()));
            continue;
        }
        if !known(&name, fixture) {
            return Err("Unknown instance files retained for review");
        }
        let proof = file_proof(dir, &name)?;
        files.insert(name, proof);
        if files.len() > 64 {
            return Err("Too many instance files; retaining evidence");
        }
    }
    current_directory(dir)?;
    Ok((files, control))
}
fn retire_control_socket(p: &Platform, dir: &Directory) -> Result<()> {
    use std::os::unix::{fs::FileTypeExt, net::UnixDatagram};
    // Called only after verify-cleanup proves all recorded daemons have exited.
    // An interrupted hostapd can leave its filesystem socket behind.
    daemons::check(p, dir)?;
    let path = dir.path.join("hostapd_ctrl");
    match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("Cannot inspect hostapd control directory"),
        Ok(_) => {}
    }
    let control = Directory::open(&path)?;
    let (data, _) = snapshot(p, dir)?;
    let iface = &data.fields["WIFI_IFACE"];
    interface_name(iface)?;
    let held = PathBuf::from(format!("/proc/self/fd/{}", control.fd.as_raw_fd()));
    let entries = fs::read_dir(&held)
        .map_err(|_| "Cannot inspect control directory contents")?
        .collect::<std::io::Result<Vec<_>>>()
        .map_err(|_| "Cannot inspect control directory entry")?;
    if entries.is_empty() {
        return Ok(());
    }
    if entries.len() != 1 || entries[0].file_name() != iface.as_str() {
        return Err("Unknown hostapd control entries retained");
    }
    let socket = held.join(iface);
    let before = fs::symlink_metadata(&socket)
        .map_err(|_| "Cannot inspect hostapd control socket")?;
    if !before.file_type().is_socket()
        || before.uid() != unsafe { libc::geteuid() }
        || before.nlink() != 1
        || before.mode() & 0o002 != 0
    {
        return Err("Unsafe hostapd control entry retained");
    }
    let probe = UnixDatagram::unbound().map_err(|_| "Cannot probe control socket")?;
    match probe.connect(&socket) {
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {}
        _ => return Err("Live or unconfirmed hostapd control socket retained"),
    }
    current_directory(dir)?;
    current_directory(&control)?;
    let after = fs::symlink_metadata(&socket)
        .map_err(|_| "Control socket disappeared before retirement")?;
    if (before.dev(), before.ino(), before.mode(), before.uid(), before.nlink())
        != (after.dev(), after.ino(), after.mode(), after.uid(), after.nlink())
    {
        return Err("Control socket changed before retirement");
    }
    let name = filename(iface)?;
    if unsafe { libc::unlinkat(control.fd.as_raw_fd(), name.as_ptr(), 0) } != 0 {
        return Err("Cannot retire stale hostapd control socket");
    }
    control.fd.sync_all().map_err(|_| "Cannot sync control socket retirement")
}
async fn cleanup(p: &Platform, path: &Path) -> Result<()> {
    if completion::recover(p, path, false).await? {
        return Ok(());
    }
    let dir = p.open(path, false)?;
    snapshot(p, &dir)?;
    let _lock = dir.transaction_lock(c".instance.lock").await?;
    stop_children(p, &dir).await?;
    if let Some(root) = &p.fixture {
        if root.join("fail-cleanup").exists() {
            return Err("Simulated instance cleanup failure");
        }
    }
    action(p, "check-interfaces", path, &[]).await?;
    let mut failure = None;
    for operation in [
        "firewall-remove",
        "forwarding-release",
        "interface-remove",
        "device-restore",
        "verify-cleanup",
    ] {
        if let Err(error) = action(p, operation, path, &[]).await {
            failure.get_or_insert(error);
        }
    }
    if let Some(error) = failure {
        return Err(error);
    }
    retire_control_socket(p, &dir)?;
    completion::create(p, &dir).await
}

pub(super) async fn run(p: &Platform, args: &[String]) -> Result<()> {
    unsafe {
        libc::umask(0o077);
    }
    match parse(args)? {
        Options::Recover(path) => {
            if completion::recover(p, &path, true).await? {
                return Ok(());
            }
            let dir = p.open(&path, false)?;
            let (data, _) = snapshot(p, &dir)?;
            if data.owner.current()?.is_some() {
                return Err("Refusing instance recovery while its owner is alive");
            }
            cleanup(p, &path).await
        }
        Options::Start {
            config,
            marker,
            band,
            channel,
            bridge,
        } => {
            // Register before publishing any state or starting network operations.
            let mut stop = signal(SignalKind::user_defined1())
                .map_err(|_| "Cannot register instance stop signal")?;
            let mut term = signal(SignalKind::terminate())
                .map_err(|_| "Cannot register instance termination signal")?;
            let mut interrupt = signal(SignalKind::interrupt())
                .map_err(|_| "Cannot register instance interrupt signal")?;
            let mut fail = signal(SignalKind::user_defined2())
                .map_err(|_| "Cannot register instance failure signal")?;
            let (config, root, token) = options(p, &config, &marker, &band, &channel, bridge)?;
            let path = publish(p, &config, &marker, &token).await?;
            let result = tokio::select! {
                result = start(p, &path, &root, &marker, &band, &channel) => result,
                _ = stop.recv() => Ok(()), _ = term.recv() => Ok(()), _ = interrupt.recv() => Ok(()),
                _ = fail.recv() => Err("Native instance failure signal"),
            };
            cleanup(p, &path).await?;
            result
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_options_are_complete_unique_and_literal() {
        let input = [
            "--config",
            "/private/path with spaces.conf",
            "--pidfile",
            "/private/marker.pid",
            "--freq-band",
            "2.4",
            "-c",
            "6",
            "--daemon",
            "--no-haveged",
        ]
        .map(String::from)
        .to_vec();
        assert!(matches!(parse(&input), Ok(Options::Start { .. })));
        for extra in [
            vec!["--config", "secret"],
            vec!["--country", "secret"],
            vec!["password-secret"],
        ] {
            let mut args = input.clone();
            args.extend(extra.into_iter().map(String::from));
            let error = parse(&args).unwrap_err();
            assert!(!error.contains("secret"));
        }
        for length in 0..input.len() {
            assert!(parse(&input[..length]).is_err());
        }
        let mut args = input.clone();
        args[7] = "01".into();
        assert!(parse(&args).is_err());
        assert_eq!(
            parse(&["--recover".into(), "/instance".into()]).unwrap(),
            Options::Recover("/instance".into())
        );
    }
}
