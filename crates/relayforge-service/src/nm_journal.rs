//! Credential-free write-ahead ownership journal, locked for the service lifetime.
use crate::config_store::random_secret;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::{
    ffi::CString,
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::Path,
};
pub type Result<T> = std::result::Result<T, &'static str>;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Owned {
    pub version: u32,
    pub token: String,
    pub uuid: String,
    pub interface: String,
    pub parent: String,
    pub phy: u32,
    pub helper: bool,
    pub mac: String,
    pub ifindex: Option<u32>,
}
impl Owned {
    pub fn new(parent: &str, phy: u32, helper: bool) -> Result<Self> {
        let token = random_secret()?;
        let hex = random_secret()?;
        let uuid = format!(
            "{}-{}-4{}-a{}-{}",
            &hex[..8],
            &hex[8..12],
            &hex[13..16],
            &hex[17..20],
            &hex[20..]
        );
        let mac = format!(
            "02:{}:{}:{}:{}:{}",
            &token[..2],
            &token[2..4],
            &token[4..6],
            &token[6..8],
            &token[8..10]
        );
        let record = Self {
            version: 1,
            token: token.clone(),
            uuid,
            interface: format!("rf{}", &token[..10]),
            parent: parent.into(),
            phy,
            helper,
            mac,
            ifindex: None,
        };
        record.validate()?;
        Ok(record)
    }
    pub fn validate(&self) -> Result<()> {
        let hex = |value: &str| {
            value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        let parts: Vec<_> = self.uuid.split('-').collect();
        if self.version != 1
            || self.token.len() != 32
            || !hex(&self.token)
            || self.interface != format!("rf{}", &self.token[..10])
            || parts.iter().map(|v| v.len()).collect::<Vec<_>>() != [8, 4, 4, 4, 12]
            || !parts.iter().all(|v| hex(v))
            || self.mac.split(':').map(|v| v.len()).collect::<Vec<_>>() != [2; 6]
            || !self.mac.split(':').all(hex)
            || self.helper && &self.mac[..2] != "02"
            || self.ifindex == Some(0)
        {
            return Err("Invalid native ownership record");
        }
        relayforge_core::config::parse_patch(
            &serde_json::json!({"WIFI_IFACE":self.parent}).to_string(),
        )?;
        if self.parent == self.interface {
            return Err("Invalid native ownership parent");
        }
        Ok(())
    }
}
pub trait Record: Serialize + DeserializeOwned {
    fn validate_record(&self) -> Result<()>;
}
impl Record for Owned {
    fn validate_record(&self) -> Result<()> {
        self.validate()
    }
}

pub struct Journal<T: Record = Owned> {
    name: CString,
    record: std::marker::PhantomData<T>,
    directory: File,
    _lock: File,
    uid: u32,
    expected: Option<Vec<u8>>,
}
impl<T: Record> Journal<T> {
    pub fn open(path: &Path, uid: u32) -> Result<Self> {
        Self::open_named(path, uid, c"networkmanager.json")
    }
    pub fn open_named(path: &Path, uid: u32, name: &std::ffi::CStr) -> Result<Self> {
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "Cannot open ownership directory")?;
        let meta = directory
            .metadata()
            .map_err(|_| "Cannot inspect ownership directory")?;
        if meta.uid() != uid || meta.mode() & 0o7777 != 0o700 {
            return Err("Ownership directory must be private and owned by service user");
        }
        let mut journal = Self {
            name: name.to_owned(),
            record: std::marker::PhantomData,
            directory,
            _lock: File::open("/dev/null").map_err(|_| "Cannot initialize ownership lock")?,
            uid,
            expected: None,
        };
        let lock = journal.open_file(c".lock", libc::O_RDWR | libc::O_CREAT)?;
        // SAFETY: flock acts on the owned regular file descriptor.
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("Native ownership directory is already locked");
        }
        journal._lock = lock;
        // A stopped service may still own resources. A different mode must not
        // bypass its pending recovery merely by using a different journal name.
        let other = if name == c"create-ap.json" {
            c"networkmanager.json"
        } else {
            c"create-ap.json"
        };
        if journal.read_private(other, 65536)?.is_some() {
            return Err("Clean up the previous native backend before switching service modes");
        }
        journal.expected = journal.bytes()?;
        if let Some(bytes) = &journal.expected {
            let record: T =
                serde_json::from_slice(bytes).map_err(|_| "Invalid native ownership journal")?;
            record.validate_record()?;
        }
        Ok(journal)
    }
    fn open_file(&self, name: &std::ffi::CStr, flags: i32) -> Result<File> {
        // SAFETY: dirfd/name are valid and returned descriptor is owned below.
        let fd = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("Cannot open native ownership file");
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect native ownership file")?;
        if !meta.is_file()
            || meta.uid() != self.uid
            || meta.mode() & 0o7777 != 0o600
            || meta.nlink() != 1
        {
            return Err("Unsafe native ownership file");
        }
        Ok(file)
    }
    fn bytes(&self) -> Result<Option<Vec<u8>>> {
        // SAFETY: valid dirfd and constant filename. Only ENOENT means absent.
        let fd = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                self.name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                Ok(None)
            } else {
                Err("Cannot read native ownership journal")
            };
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect native ownership journal")?;
        if !meta.is_file()
            || meta.uid() != self.uid
            || meta.mode() & 0o7777 != 0o600
            || meta.nlink() != 1
            || meta.len() > 16384
        {
            return Err("Unsafe native ownership journal");
        }
        let mut bytes = Vec::new();
        file.take(16385)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read native ownership journal")?;
        if bytes.len() > 16384 {
            return Err("Oversized native ownership journal");
        }
        Ok(Some(bytes))
    }
    pub fn load(&self) -> Result<Option<T>> {
        if self.bytes()? != self.expected {
            return Err("Native ownership journal changed externally");
        }
        self.expected
            .as_ref()
            .map(|bytes| {
                serde_json::from_slice(bytes).map_err(|_| "Invalid native ownership journal")
            })
            .transpose()
    }
    pub fn write(&mut self, owned: &T) -> Result<()> {
        owned.validate_record()?;
        if self.bytes()? != self.expected {
            return Err("Native ownership journal changed externally");
        }
        let bytes =
            serde_json::to_vec(owned).map_err(|_| "Cannot encode native ownership journal")?;
        let name = CString::new(format!(".journal-{}", random_secret()?)).unwrap();
        let result = (|| {
            let mut file = self.open_file(&name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
            file.write_all(&bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| "Cannot persist native ownership journal")?;
            if self.bytes()? != self.expected {
                return Err("Native ownership journal changed externally");
            }
            // SAFETY: both paths are relative to the retained private directory.
            if unsafe {
                libc::renameat(
                    self.directory.as_raw_fd(),
                    name.as_ptr(),
                    self.directory.as_raw_fd(),
                    self.name.as_ptr(),
                )
            } != 0
            {
                return Err("Cannot commit native ownership journal");
            }
            self.expected = Some(bytes);
            self.directory
                .sync_all()
                .map_err(|_| "Cannot sync native ownership directory")
        })();
        unsafe {
            libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0);
        }
        result
    }
    pub fn create_private(&self, name: &std::ffi::CStr, bytes: &[u8]) -> Result<()> {
        let mut file = self.open_file(name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
        let result = file
            .write_all(bytes)
            .and_then(|_| file.sync_all())
            .and_then(|_| self.directory.sync_all())
            .map_err(|_| "Cannot persist private runtime file");
        if result.is_err() {
            unsafe {
                libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0);
            }
        }
        result
    }
    pub fn read_private(&self, name: &std::ffi::CStr, limit: usize) -> Result<Option<Vec<u8>>> {
        // SAFETY: filename and retained directory descriptor are valid.
        let fd = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                Ok(None)
            } else {
                Err("Cannot read private runtime file")
            };
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let meta = file
            .metadata()
            .map_err(|_| "Cannot inspect private runtime file")?;
        if !meta.is_file()
            || meta.uid() != self.uid
            || meta.nlink() != 1
            || meta.mode() & 0o7777 != 0o600
            || meta.len() > limit as u64
        {
            return Err("Unsafe private runtime file");
        }
        let mut bytes = Vec::new();
        file.take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read private runtime file")?;
        if bytes.len() > limit {
            return Err("Oversized private runtime file");
        }
        Ok(Some(bytes))
    }
    pub fn remove_private(&self, name: &std::ffi::CStr) -> Result<()> {
        if self.read_private(name, 65536)?.is_some() {
            if unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) } != 0 {
                return Err("Cannot remove private runtime file");
            }
            self.directory
                .sync_all()
                .map_err(|_| "Cannot sync private runtime directory")?;
        }
        Ok(())
    }
    pub fn clear(&mut self) -> Result<()> {
        if self.bytes()? != self.expected {
            return Err("Native ownership journal changed externally");
        }
        if self.expected.is_some() {
            if unsafe { libc::unlinkat(self.directory.as_raw_fd(), self.name.as_ptr(), 0) } != 0 {
                return Err("Cannot remove native ownership journal");
            }
            self.expected = None;
            self.directory
                .sync_all()
                .map_err(|_| "Cannot sync native ownership directory")?;
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};
    #[test]
    fn locks_validates_and_preserves_external_edits() {
        let path =
            std::env::temp_dir().join(format!("relayforge-journal-{}", random_secret().unwrap()));
        std::fs::create_dir(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        let uid = unsafe { libc::geteuid() };
        let mut journal = Journal::<Owned>::open(&path, uid).unwrap();
        assert!(Journal::<Owned>::open(&path, uid).is_err());
        let record = Owned::new("wlan0", 0, true).unwrap();
        journal.write(&record).unwrap();
        assert_eq!(journal.load().unwrap(), Some(record.clone()));
        std::fs::write(path.join("networkmanager.json"), b"external").unwrap();
        assert!(journal.clear().is_err());
        assert!(journal.write(&record).is_err());
        drop(journal);
        assert!(Journal::<Owned>::open(&path, uid).is_err());
        std::fs::remove_file(path.join("networkmanager.json")).unwrap();
        symlink("/dev/null", path.join("networkmanager.json")).unwrap();
        assert!(Journal::<Owned>::open(&path, uid).is_err());
        std::fs::remove_dir_all(path).unwrap();
    }
    #[test]
    fn private_snapshots_and_pending_backend_journals_fail_closed() {
        let path =
            std::env::temp_dir().join(format!("relayforge-journal-{}", random_secret().unwrap()));
        std::fs::create_dir(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        let uid = unsafe { libc::geteuid() };
        let journal = Journal::<Owned>::open(&path, uid).unwrap();
        journal.create_private(c"snapshot", b"private").unwrap();
        assert!(journal.create_private(c"snapshot", b"replace").is_err());
        assert_eq!(
            journal.read_private(c"snapshot", 32).unwrap().unwrap(),
            b"private"
        );
        assert!(journal.read_private(c"snapshot", 2).is_err());
        std::fs::hard_link(path.join("snapshot"), path.join("linked")).unwrap();
        assert!(journal.remove_private(c"snapshot").is_err());
        std::fs::remove_file(path.join("linked")).unwrap();
        journal.remove_private(c"snapshot").unwrap();
        symlink("/dev/null", path.join("snapshot")).unwrap();
        assert!(journal.read_private(c"snapshot", 32).is_err());
        assert!(journal.remove_private(c"snapshot").is_err());
        journal
            .create_private(c"create-ap.json", b"pending")
            .unwrap();
        drop(journal);
        assert!(Journal::<Owned>::open(&path, uid).is_err());
        std::fs::remove_file(path.join("create-ap.json")).unwrap();
        let mut journal = Journal::<Owned>::open(&path, uid).unwrap();
        journal
            .write(&Owned::new("wlan0", 0, true).unwrap())
            .unwrap();
        drop(journal);
        assert!(Journal::<Owned>::open_named(&path, uid, c"create-ap.json").is_err());
        std::fs::remove_dir_all(path).unwrap();
    }
}
