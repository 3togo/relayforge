//! Native credential storage. All filesystem access runs on a blocking worker.
use relayforge_core::config::{self, Config, MAX_CONFIG};
use std::{
    ffi::{CStr, CString},
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::Path,
};

pub enum Store {
    Memory(Config),
    Disk(ConfigFile),
}

impl Store {
    pub fn load(&self) -> Result<Config, &'static str> {
        match self {
            Self::Memory(config) => Ok(config.clone()),
            Self::Disk(file) => file.read().map(|(config, _)| config),
        }
    }
    pub fn save(&mut self, patch: &Config, change_allowed: bool) -> Result<(), &'static str> {
        let (current, stamp) = match self {
            Self::Memory(config) => (config.clone(), None),
            Self::Disk(file) => {
                let (config, stamp) = file.read()?;
                (config, Some(stamp))
            }
        };
        let merged = config::merge(&current, patch)?;
        if current["BACKEND"] != merged["BACKEND"] && !change_allowed {
            return Err("Stop the hotspot before changing its backend");
        }
        match self {
            Self::Memory(config) => *config = merged,
            Self::Disk(file) => {
                file.commit(&merged, stamp.as_ref())?;
            }
        }
        Ok(())
    }
}

#[derive(PartialEq)]
struct Stamp {
    device: u64,
    inode: u64,
    size: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}
impl Stamp {
    fn from(meta: &std::fs::Metadata) -> Self {
        Self {
            device: meta.dev(),
            inode: meta.ino(),
            size: meta.len(),
            modified: (meta.mtime(), meta.mtime_nsec()),
            changed: (meta.ctime(), meta.ctime_nsec()),
        }
    }
}

/// The only failure that means "nothing has been configured yet".
pub const MISSING: &str = "Configuration is missing; initialize it with --init-config";

pub struct ConfigFile {
    directory: File,
    name: CString,
    uid: u32,
}

/// Migration reports describe actions without exposing stored credentials.
#[derive(Debug, Default, serde::Serialize)]
pub struct Migration {
    pub factory_password: bool,
    pub interfaces: bool,
    pub applied: bool,
}

// Exact source-only factory template from Wi-Fi Relay revision 12f9f78.
// A customized file is never eligible for automatic password rotation.
const LEGACY_FACTORY: &str = include_str!("../../../compat/factory.conf");

pub fn random_secret() -> Result<String, &'static str> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| "Cannot obtain secure random bytes")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

impl ConfigFile {
    pub fn open(path: &Path, uid: u32) -> Result<Self, &'static str> {
        if !path.is_absolute() {
            return Err("Configuration path must be absolute");
        }
        let parent = path.parent().ok_or("Missing configuration directory")?;
        let name = CString::new(
            path.file_name()
                .ok_or("Missing configuration filename")?
                .as_encoded_bytes(),
        )
        .map_err(|_| "Invalid configuration filename")?;
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(parent)
            .map_err(|_| "Cannot open configuration directory")?;
        let meta = directory
            .metadata()
            .map_err(|_| "Cannot inspect configuration directory")?;
        if meta.uid() != uid || meta.mode() & 0o022 != 0 {
            return Err("Configuration directory must be owned by the service user and not group/world writable");
        }
        Ok(Self {
            directory,
            name,
            uid,
        })
    }

    fn open_target(&self) -> Result<File, &'static str> {
        // SAFETY: directory is an owned open directory; name is a valid C string.
        let fd = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                self.name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            // A fresh installation has no configuration at all. A distinct message lets
            // the service exit with a status that systemd must not restart.
            if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                return Err(MISSING);
            }
            return Err("Cannot open private configuration file");
        }
        // SAFETY: successful openat returns a new owned descriptor.
        let file = unsafe { File::from_raw_fd(fd) };
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect configuration file")?;
        if !meta.is_file()
            || meta.uid() != self.uid
            || meta.mode() & 0o7777 != 0o600
            || meta.nlink() != 1
        {
            return Err("Configuration must be a single-link regular file owned by the service user with mode 0600");
        }
        Ok(file)
    }

    fn read_text(&self) -> Result<(String, Stamp), &'static str> {
        let file = self.open_target()?;
        let before = file
            .metadata()
            .map_err(|_| "Cannot inspect configuration file")?;
        let stamp = Stamp::from(&before);
        if before.len() > MAX_CONFIG as u64 {
            return Err("Configuration exceeds 64 KiB");
        }
        let mut text = String::new();
        (&file)
            .take(MAX_CONFIG as u64 + 1)
            .read_to_string(&mut text)
            .map_err(|_| "Cannot read UTF-8 configuration")?;
        if Stamp::from(
            &file
                .metadata()
                .map_err(|_| "Cannot inspect configuration file")?,
        ) != stamp
        {
            return Err("Configuration changed while reading; retry");
        }
        if text.len() > MAX_CONFIG {
            return Err("Configuration exceeds 64 KiB");
        }
        Ok((text, stamp))
    }

    fn read(&self) -> Result<(Config, Stamp), &'static str> {
        let (text, stamp) = self.read_text()?;
        let raw = config::parse_records(&text)?;
        if !raw.contains_key("PASSPHRASE") {
            return Err("Stored configuration must explicitly contain PASSPHRASE");
        }
        let config = config::validate(&raw)?;
        Ok((config, stamp))
    }

    fn temporary(&self) -> Result<(CString, File), &'static str> {
        let name = CString::new(format!(".relayforge-{}", random_secret()?)).unwrap();
        // SAFETY: directory and name are valid; O_EXCL prevents reusing a path.
        let fd = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("Cannot create private configuration temporary file");
        }
        // SAFETY: successful openat transferred a new owned descriptor.
        Ok((name, unsafe { File::from_raw_fd(fd) }))
    }

    fn unlink(&self, name: &CStr) {
        // SAFETY: name is an owned temporary filename relative to our directory.
        unsafe {
            libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0);
        }
    }

    fn commit(&self, config: &Config, expected: Option<&Stamp>) -> Result<bool, &'static str> {
        let text = config::serialize_file(config)?;
        self.commit_text(&text, expected)
    }

    fn commit_text(&self, text: &str, expected: Option<&Stamp>) -> Result<bool, &'static str> {
        let (name, mut file) = self.temporary()?;
        let result = (|| {
            file.set_permissions(std::fs::Permissions::from_mode(0o600))
                .map_err(|_| "Cannot protect configuration temporary file")?;
            file.write_all(text.as_bytes())
                .and_then(|_| file.sync_all())
                .map_err(|_| "Cannot write and sync configuration temporary file")?;
            if let Some(expected) = expected {
                let current = self
                    .open_target()?
                    .metadata()
                    .map_err(|_| "Cannot inspect current configuration")?;
                if Stamp::from(&current) != *expected {
                    return Err("Configuration changed externally; reload before saving");
                }
            }
            let flags = if expected.is_some() {
                0
            } else {
                libc::RENAME_NOREPLACE
            };
            // SAFETY: both names are C strings relative to the retained directory.
            let outcome = unsafe {
                libc::renameat2(
                    self.directory.as_raw_fd(),
                    name.as_ptr(),
                    self.directory.as_raw_fd(),
                    self.name.as_ptr(),
                    flags,
                )
            };
            if outcome != 0 {
                if expected.is_none()
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::EEXIST)
                {
                    return Ok(false);
                }
                return Err("Cannot atomically commit configuration");
            }
            self.directory.sync_all().map_err(|_| {
                "Configuration replaced but directory sync failed; reload before retrying"
            })?;
            Ok(true)
        })();
        self.unlink(&name);
        result
    }

    #[cfg(test)]
    fn update(&self, patch: &Config) -> Result<(), &'static str> {
        let (current, stamp) = self.read()?;
        let config = config::merge(&current, patch)?;
        self.commit(&config, Some(&stamp))?;
        Ok(())
    }

    /// Raw private bytes plus the group an atomic replacement must preserve.
    fn bytes(&self) -> Result<(Vec<u8>, u32), &'static str> {
        let file = self.open_target()?;
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect configuration file")?;
        if meta.len() > MAX_CONFIG as u64 {
            return Err("Configuration exceeds 64 KiB");
        }
        let mut bytes = Vec::new();
        (&file)
            .take(MAX_CONFIG as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read configuration file")?;
        if bytes.len() > MAX_CONFIG {
            return Err("Configuration exceeds 64 KiB");
        }
        Ok((bytes, meta.gid()))
    }

    /// Replace the private file with literal bytes, preserving owner and group.
    fn replace_bytes(&self, bytes: &[u8], gid: u32) -> Result<(), &'static str> {
        let (name, mut file) = self.temporary()?;
        let result = (|| {
            file.set_permissions(std::fs::Permissions::from_mode(0o600))
                .map_err(|_| "Cannot protect configuration temporary file")?;
            // SAFETY: file is an owned descriptor created in the retained directory.
            if unsafe { libc::fchown(file.as_raw_fd(), self.uid, gid) } != 0 {
                return Err("Cannot preserve configuration file ownership");
            }
            file.write_all(bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| "Cannot write and sync configuration temporary file")?;
            // SAFETY: both names are C strings relative to the retained directory.
            let outcome = unsafe {
                libc::renameat2(
                    self.directory.as_raw_fd(),
                    name.as_ptr(),
                    self.directory.as_raw_fd(),
                    self.name.as_ptr(),
                    0,
                )
            };
            if outcome != 0 {
                return Err("Cannot atomically replace configuration");
            }
            self.directory.sync_all().map_err(|_| {
                "Configuration replaced but directory sync failed; reload before retrying"
            })?;
            Ok(())
        })();
        self.unlink(&name);
        result
    }

    /// Port of the pinned installer repair: replace an obsolete `wlan0` default
    /// only when `wlan0` is absent and exactly one Wi-Fi adapter (or an explicit
    /// interface) is available. Every other line, comment and assignment is kept
    /// byte-for-byte so an upgrade cannot silently rewrite settings.
    pub fn repair_interface(
        &self,
        interface: Option<&str>,
        net: &Path,
    ) -> Result<bool, &'static str> {
        if net.join("wlan0").exists() {
            return Ok(false);
        }
        let Some(interface) = interface
            .map(str::to_owned)
            .or_else(|| crate::inventory::unique_wifi(net))
        else {
            return Ok(false);
        };
        config::validate(&Config::from([
            ("WIFI_IFACE".into(), interface.clone()),
            ("INTERNET_IFACE".into(), interface.clone()),
        ]))?;
        let (original, gid) = self.bytes()?;
        let mut used = [false; 3];
        let mut lines = Vec::new();
        for line in original.split(|byte| *byte == b'\n') {
            let (body, carriage) = match line.strip_suffix(b"\r") {
                Some(body) => (body, true),
                None => (line, false),
            };
            let mut updated = body.to_vec();
            // The historical installer could leave the first assignment as a literal.
            if !used[0] && body == b"WIFI_IFACE=INTERNET_IFACE=wlan0" {
                updated = format!("WIFI_IFACE={interface}").into_bytes();
                used[0] = true;
            } else if !used[1] && body == b"WIFI_IFACE=wlan0" {
                updated = format!("WIFI_IFACE={interface}").into_bytes();
                used[1] = true;
            } else if !used[2] && body == b"INTERNET_IFACE=wlan0" {
                updated = format!("INTERNET_IFACE={interface}").into_bytes();
                used[2] = true;
            }
            if carriage {
                updated.push(b'\r');
            }
            lines.push(updated);
        }
        let updated = lines.join(&b'\n');
        if updated == original {
            return Ok(false);
        }
        self.replace_bytes(&updated, gid)?;
        Ok(true)
    }

    pub fn initialize(&self, ssid: &str, interface: Option<&str>) -> Result<bool, &'static str> {
        let mut config = config::defaults();
        config.insert("SSID".into(), ssid.into());
        config.insert("PASSPHRASE".into(), random_secret()?);
        if let Some(iface) = interface {
            config.insert("WIFI_IFACE".into(), iface.into());
            config.insert("INTERNET_IFACE".into(), iface.into());
        }
        self.commit(&config, None)
    }

    /// Explicit offline migration; callers supply an unambiguous replacement
    /// only after verifying the legacy wlan0 device is absent. No network I/O.
    pub fn migrate_legacy(
        &self,
        interface: Option<&str>,
        dry_run: bool,
    ) -> Result<Migration, &'static str> {
        if let Some(interface) = interface {
            config::validate(&Config::from([("WIFI_IFACE".into(), interface.into())]))?;
        }
        let (text, stamp) = self.read_text()?;
        let mut outcome = Migration {
            factory_password: text == LEGACY_FACTORY,
            ..Migration::default()
        };
        let mut updated = if outcome.factory_password {
            text.replacen(
                "PASSPHRASE=12345678\n",
                &format!("PASSPHRASE={}\n", random_secret()?),
                1,
            )
        } else {
            text.clone()
        };
        if let Some(interface) = interface {
            let mut repaired = String::new();
            for line in updated.split_inclusive('\n') {
                let record = line.trim_end_matches(['\r', '\n']);
                let key = match record {
                    "WIFI_IFACE=wlan0" | "WIFI_IFACE=INTERNET_IFACE=wlan0" => "WIFI_IFACE",
                    "INTERNET_IFACE=wlan0" => "INTERNET_IFACE",
                    _ => {
                        repaired.push_str(line);
                        continue;
                    }
                };
                repaired.push_str(&format!("{key}={interface}"));
                repaired.push_str(&line[record.len()..]);
            }
            outcome.interfaces = repaired != updated;
            updated = repaired;
        }
        if updated == text {
            return Ok(outcome);
        }
        let raw = config::parse_records(&updated)?;
        if !raw.contains_key("PASSPHRASE") {
            return Err("Stored configuration must explicitly contain PASSPHRASE");
        }
        config::validate(&raw)?;
        if !dry_run {
            self.commit_text(&updated, Some(&stamp))?;
            outcome.applied = true;
        }
        Ok(outcome)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "relayforge-store-{}-{}",
                std::process::id(),
                random_secret().unwrap()
            ));
            std::fs::create_dir(&path).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }
        fn path(&self) -> std::path::PathBuf {
            self.0.join("config")
        }
        fn file(&self) -> ConfigFile {
            ConfigFile::open(&self.path(), unsafe { libc::geteuid() }).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn initialize_is_private_random_and_never_overwrites_existing_credentials() {
        let root = Fixture::new();
        let file = root.file();
        assert!(file.initialize("Demo", Some("wlp2s0")).unwrap());
        let original = std::fs::read(root.path()).unwrap();
        let (config, _) = file.read().unwrap();
        assert_eq!(config["PASSPHRASE"].len(), 32);
        assert_ne!(config["PASSPHRASE"], "12345678");
        assert_eq!(config["WIFI_IFACE"], "wlp2s0");
        assert!(!file.initialize("Replacement", None).unwrap());
        assert_eq!(std::fs::read(root.path()).unwrap(), original);
        let other = Fixture::new();
        other.file().initialize("Demo", None).unwrap();
        assert_ne!(
            config["PASSPHRASE"],
            other.file().read().unwrap().0["PASSPHRASE"]
        );
    }
    #[test]
    fn invalid_update_and_backend_change_preserve_exact_file_bytes() {
        let root = Fixture::new();
        let file = root.file();
        file.initialize("Demo", None).unwrap();
        let before = std::fs::read(root.path()).unwrap();
        let mut store = Store::Disk(file);
        assert!(store
            .save(&Config::from([("USE_PSK".into(), "1".into())]), true)
            .is_err());
        assert!(store
            .save(
                &Config::from([("BACKEND".into(), "networkmanager".into())]),
                false
            )
            .is_err());
        assert_eq!(std::fs::read(root.path()).unwrap(), before);
    }
    #[test]
    fn atomic_update_preserves_password_and_cleans_temporary_files() {
        let root = Fixture::new();
        let file = root.file();
        file.initialize("Demo", None).unwrap();
        let password = file.read().unwrap().0["PASSPHRASE"].clone();
        let inode = std::fs::metadata(root.path()).unwrap().ino();
        file.update(&Config::from([("SSID".into(), "Changed".into())]))
            .unwrap();
        assert_ne!(std::fs::metadata(root.path()).unwrap().ino(), inode);
        assert_eq!(file.read().unwrap().0["PASSPHRASE"], password);
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 1);
    }
    #[test]
    fn refuses_symlinks_hardlinks_wrong_owner_and_insecure_modes() {
        let root = Fixture::new();
        let other = Fixture::new();
        other.file().initialize("Other", None).unwrap();
        std::os::unix::fs::symlink(other.path(), root.path()).unwrap();
        assert!(root.file().read().is_err());
        assert!(!root.file().initialize("New", None).unwrap());
        std::fs::remove_file(root.path()).unwrap();
        std::fs::hard_link(other.path(), root.path()).unwrap();
        assert!(root.file().read().is_err());
        std::fs::remove_file(root.path()).unwrap();
        root.file().initialize("Demo", None).unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(root.file().read().is_err());
        assert!(
            ConfigFile::open(&root.path(), unsafe { libc::geteuid() }.wrapping_add(1)).is_err()
        );
    }
    #[test]
    fn rejects_fifo_oversized_data_and_non_utf8_without_blocking() {
        let root = Fixture::new();
        let path = CString::new(root.path().as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        assert!(root.file().read().is_err());
        std::fs::remove_file(root.path()).unwrap();
        std::fs::write(root.path(), vec![b'x'; MAX_CONFIG + 1]).unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(root.file().read().is_err());
        std::fs::write(root.path(), [0xff]).unwrap();
        assert!(root.file().read().is_err());
    }
    #[test]
    fn stale_save_does_not_overwrite_an_external_edit() {
        let root = Fixture::new();
        let file = root.file();
        file.initialize("Demo", None).unwrap();
        let (config, stamp) = file.read().unwrap();
        file.update(&Config::from([("SSID".into(), "External".into())]))
            .unwrap();
        assert!(file.commit(&config, Some(&stamp)).is_err());
        assert_eq!(file.read().unwrap().0["SSID"], "External");
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 1);
    }

    #[test]
    fn interface_repair_touches_only_the_obsolete_default() {
        let root = Fixture::new();
        let net = root.0.join("net");
        std::fs::create_dir_all(net.join("wlp4s0/wireless")).unwrap();
        // A historical installer could leave the first assignment as a literal.
        std::fs::write(
            root.path(),
            "WIFI_IFACE=INTERNET_IFACE=wlan0\nINTERNET_IFACE=wlan0\nSSID=Mine\nPASSPHRASE=private-password\n",
        )
        .unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o600)).unwrap();
        let file = root.file();
        assert!(file.repair_interface(None, &net).unwrap());
        assert_eq!(
            std::fs::read_to_string(root.path()).unwrap(),
            "WIFI_IFACE=wlp4s0\nINTERNET_IFACE=wlp4s0\nSSID=Mine\nPASSPHRASE=private-password\n"
        );
        assert_eq!(std::fs::metadata(root.path()).unwrap().mode() & 0o777, 0o600);
        assert!(!file.repair_interface(None, &net).unwrap());
        // Ambiguous hardware is refused; an explicit interface still repairs.
        std::fs::create_dir_all(net.join("wlp5s0/wireless")).unwrap();
        std::fs::write(root.path(), "WIFI_IFACE=wlan0\nINTERNET_IFACE=wlan0\n").unwrap();
        assert!(!file.repair_interface(None, &net).unwrap());
        assert!(file.repair_interface(Some("wlp5s0"), &net).unwrap());
        assert_eq!(
            std::fs::read_to_string(root.path()).unwrap(),
            "WIFI_IFACE=wlp5s0\nINTERNET_IFACE=wlp5s0\n"
        );
        // A present wlan0 is never rewritten, and an invalid name never writes.
        std::fs::create_dir(net.join("wlan0")).unwrap();
        assert!(!file.repair_interface(Some("wlp5s0"), &net).unwrap());
        std::fs::remove_dir(net.join("wlan0")).unwrap();
        let before = std::fs::read(root.path()).unwrap();
        assert!(file.repair_interface(Some("-bad name"), &net).is_err());
        assert_eq!(std::fs::read(root.path()).unwrap(), before);
    }

    #[test]
    fn missing_password_never_falls_back_to_the_historical_factory_value() {
        let root = Fixture::new();
        root.file().initialize("Demo", None).unwrap();
        for text in ["", "SSID=Incomplete\n"] {
            std::fs::write(root.path(), text).unwrap();
            assert!(root.file().read().is_err());
        }
    }

    fn legacy_file(root: &Fixture, text: &str) {
        std::fs::write(root.path(), text).unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    #[test]
    fn factory_migration_is_explicit_private_and_idempotent() {
        let root = Fixture::new();
        legacy_file(&root, LEGACY_FACTORY);
        let report = root.file().migrate_legacy(Some("wlp2s0"), false).unwrap();
        assert!(report.factory_password && report.interfaces && report.applied);
        let text = std::fs::read_to_string(root.path()).unwrap();
        assert!(text.starts_with("# Wi-Fi Hotspot Configuration\n"));
        assert!(!text.contains("12345678"));
        assert_eq!(root.file().read().unwrap().0["WIFI_IFACE"], "wlp2s0");
        assert_eq!(
            std::fs::metadata(root.path()).unwrap().mode() & 0o777,
            0o600
        );
        let repeat = root.file().migrate_legacy(Some("wlp2s0"), false).unwrap();
        assert!(!repeat.factory_password && !repeat.interfaces && !repeat.applied);
        assert_eq!(std::fs::read_to_string(root.path()).unwrap(), text);
    }

    #[test]
    fn customized_factory_password_is_preserved_and_dry_run_never_writes() {
        let root = Fixture::new();
        let customized = LEGACY_FACTORY.replace("SSID=Hotspot", "SSID=Customized");
        legacy_file(&root, &customized);
        let report = root.file().migrate_legacy(None, false).unwrap();
        assert!(!report.factory_password && !report.applied);
        assert_eq!(std::fs::read_to_string(root.path()).unwrap(), customized);
        legacy_file(&root, LEGACY_FACTORY);
        let report = root.file().migrate_legacy(Some("wlp2s0"), true).unwrap();
        assert!(report.factory_password && report.interfaces && !report.applied);
        assert_eq!(
            std::fs::read_to_string(root.path()).unwrap(),
            LEGACY_FACTORY
        );
    }

    #[test]
    fn interface_repair_preserves_literal_credentials_comments_and_custom_interfaces() {
        let root = Fixture::new();
        let input = "# Keep this comment\r\nWIFI_IFACE=INTERNET_IFACE=wlan0\r\nINTERNET_IFACE=eth9\r\nPASSPHRASE= keep =#'password \r\n";
        legacy_file(&root, input);
        let report = root.file().migrate_legacy(Some("wlp2s0"), false).unwrap();
        assert!(report.interfaces && report.applied && !report.factory_password);
        assert_eq!(
            std::fs::read_to_string(root.path()).unwrap(),
            input.replace("WIFI_IFACE=INTERNET_IFACE=wlan0", "WIFI_IFACE=wlp2s0")
        );
    }

    #[test]
    fn unsafe_or_invalid_migrations_leave_the_original_untouched() {
        let root = Fixture::new();
        let input = "WIFI_IFACE=wlan0\nPASSPHRASE=short\n";
        legacy_file(&root, input);
        assert!(root.file().migrate_legacy(Some("wlp2s0"), false).is_err());
        assert_eq!(std::fs::read_to_string(root.path()).unwrap(), input);
        assert!(root
            .file()
            .migrate_legacy(Some("--injected"), false)
            .is_err());
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(root.file().migrate_legacy(None, false).is_err());
        assert_eq!(std::fs::read_to_string(root.path()).unwrap(), input);
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 1);
    }
}
