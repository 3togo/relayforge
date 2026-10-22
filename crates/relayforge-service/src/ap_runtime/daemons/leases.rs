//! Writable leases for an unprivileged dnsmasq without opening credential directories.
use super::*;
use std::{
    ffi::CStr,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
};
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct Account {
    pub uid: u32,
    pub gid: u32,
    pub user: String,
    pub group: String,
}
impl Account {
    pub fn validate(&self, p: &Platform) -> Result<()> {
        if (p.fixture.is_none() && (self.uid == 0 || self.gid == 0))
            || (p.fixture.is_some()
                && (self.uid != unsafe { libc::geteuid() }
                    || self.gid != unsafe { libc::getegid() }))
            || [&self.user, &self.group].iter().any(|s| {
                s.is_empty()
                    || s.len() > 64
                    || !s
                        .bytes()
                        .all(|v| v.is_ascii_alphanumeric() || b"_-".contains(&v))
            })
        {
            return Err("Invalid unprivileged DNS account");
        }
        Ok(())
    }
}
pub(super) fn account(p: &Platform) -> Result<Account> {
    if p.fixture.is_some() {
        return Ok(Account {
            uid: unsafe { libc::geteuid() },
            gid: unsafe { libc::getegid() },
            user: "fixture".into(),
            group: "fixture".into(),
        });
    }
    let mut passwd: libc::passwd = unsafe { std::mem::zeroed() };
    let mut result = std::ptr::null_mut();
    let mut buffer = vec![0i8; 16384];
    let user = c"dnsmasq";
    let status = unsafe {
        libc::getpwnam_r(
            user.as_ptr(),
            &mut passwd,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return Err("The dnsmasq system account is required");
    }
    let uid = passwd.pw_uid;
    let gid = passwd.pw_gid;
    let mut group: libc::group = unsafe { std::mem::zeroed() };
    let mut result = std::ptr::null_mut();
    let mut buffer = vec![0i8; 16384];
    let status = unsafe {
        libc::getgrgid_r(
            gid,
            &mut group,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return Err("Cannot resolve the dnsmasq system group");
    }
    let group = unsafe { CStr::from_ptr(group.gr_name) }
        .to_str()
        .map_err(|_| "Invalid dnsmasq group")?
        .to_owned();
    let account = Account {
        uid,
        gid,
        user: "dnsmasq".into(),
        group,
    };
    account.validate(p)?;
    Ok(account)
}
fn base(p: &Platform) -> PathBuf {
    p.fixture
        .as_ref()
        .map(|v| v.join("leases"))
        .unwrap_or_else(|| "/run/relayforge-leases".into())
}
pub(super) fn path(p: &Platform, marker: &str) -> Result<PathBuf> {
    let name = Path::new(marker)
        .file_name()
        .and_then(|v| v.to_str())
        .and_then(|v| v.strip_prefix("create-ap-"))
        .and_then(|v| v.strip_suffix(".pid"))
        .ok_or("Invalid lease ownership token")?;
    if name.len() != 32 || !name.bytes().all(|v| v.is_ascii_hexdigit()) {
        return Err("Invalid lease ownership token");
    }
    Ok(base(p).join(name))
}
fn base_check(p: &Platform) -> Result<()> {
    let meta = fs::symlink_metadata(base(p)).map_err(|_| "Cannot inspect lease root")?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o7777 != 0o711 {
        return Err("Unsafe lease root");
    }
    Ok(())
}
fn directory(p: &Platform, marker: &str, account: &Account) -> Result<File> {
    base_check(p)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path(p, marker)?)
        .map_err(|_| "Cannot open private lease directory")?;
    let m = file
        .metadata()
        .map_err(|_| "Cannot inspect lease directory")?;
    if m.uid() != account.uid || m.gid() != account.gid || m.mode() & 0o7777 != 0o700 {
        return Err("Unsafe lease directory ownership");
    }
    Ok(file)
}
pub(super) fn ensure(
    p: &Platform,
    marker: &str,
    account: &Account,
    expected: Option<(u64, u64)>,
) -> Result<(u64, u64)> {
    match fs::DirBuilder::new().mode(0o711).create(base(p)) {
        Ok(()) => {
            fs::set_permissions(base(p), fs::Permissions::from_mode(0o711))
                .map_err(|_| "Cannot protect lease root")?;
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err("Cannot create lease root"),
    }
    base_check(p)?;
    let path = path(p, marker)?;
    if expected.is_some() && fs::symlink_metadata(&path).is_err() {
        return Err("Owned lease directory disappeared");
    }
    match fs::DirBuilder::new().mode(0o700).create(&path) {
        Ok(()) => {
            let fd = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&path)
                .map_err(|_| "Cannot open new lease directory")?;
            if unsafe { libc::fchown(fd.as_raw_fd(), account.uid, account.gid) } != 0 {
                return Err("Cannot assign DNS lease ownership");
            }
            fd.sync_all().map_err(|_| "Cannot sync lease directory")?;
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && expected.is_some() => {}
        Err(_) => return Err("Lease directory already exists or cannot be created"),
    }
    let dir = directory(p, marker, account)?;
    let meta = dir
        .metadata()
        .map_err(|_| "Cannot inspect lease directory identity")?;
    let identity = (meta.dev(), meta.ino());
    if expected.is_some_and(|v| v != identity) {
        return Err("Lease directory identity changed");
    }
    let raw = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            c"dnsmasq.leases".as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if raw >= 0 {
        let file = unsafe { File::from_raw_fd(raw) };
        if unsafe { libc::fchown(file.as_raw_fd(), account.uid, account.gid) } != 0 {
            return Err("Cannot assign DNS lease file ownership");
        }
        file.sync_all().map_err(|_| "Cannot sync DNS lease file")?;
    } else if expected.is_none()
        || std::io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST)
    {
        return Err("Cannot create private DNS lease file");
    }
    read(p, marker, account)?;
    Ok(identity)
}
pub(super) fn verify(
    p: &Platform,
    marker: &str,
    account: &Account,
    expected: (u64, u64),
) -> Result<()> {
    let dir = directory(p, marker, account)?;
    let meta = dir
        .metadata()
        .map_err(|_| "Cannot inspect lease directory identity")?;
    if (meta.dev(), meta.ino()) != expected {
        return Err("Lease directory identity changed");
    }
    Ok(())
}
pub(super) fn read(p: &Platform, marker: &str, account: &Account) -> Result<Vec<u8>> {
    match fs::symlink_metadata(path(p, marker)?) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err("Cannot inspect private lease directory"),
        Ok(_) => {}
    }
    let dir = directory(p, marker, account)?;
    let raw = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            c"dnsmasq.leases".as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if raw < 0 {
        return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
            Ok(Vec::new())
        } else {
            Err("Cannot read private DNS leases")
        };
    }
    let file = unsafe { File::from_raw_fd(raw) };
    let m = file.metadata().map_err(|_| "Cannot inspect DNS leases")?;
    if !m.is_file()
        || m.uid() != account.uid
        || m.nlink() != 1
        || m.mode() & 0o7777 != 0o600
        || m.len() > LIMIT as u64
    {
        return Err("Unsafe DNS lease file");
    }
    let mut bytes = Vec::new();
    file.take(LIMIT as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Cannot read DNS leases")?;
    if bytes.len() > LIMIT {
        return Err("DNS leases exceed budget");
    }
    Ok(bytes)
}
pub(super) fn cleanup(p: &Platform, marker: &str, account: &Account) -> Result<()> {
    let path = path(p, marker)?;
    match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("Cannot inspect lease cleanup"),
        Ok(_) => {}
    }
    let dir = directory(p, marker, account)?;
    for entry in fs::read_dir(&path).map_err(|_| "Cannot inspect private leases")? {
        let name = entry
            .map_err(|_| "Cannot inspect private lease entry")?
            .file_name();
        if name != "dnsmasq.leases" {
            return Err("Unknown file blocks private lease cleanup");
        }
        read(p, marker, account)?;
        if unsafe { libc::unlinkat(dir.as_raw_fd(), c"dnsmasq.leases".as_ptr(), 0) } != 0 {
            return Err("Cannot remove private DNS leases");
        }
    }
    fs::remove_dir(path).map_err(|_| "Cannot remove owned lease directory")
}
