//! Descriptor-relative access. Offline images also refuse mount crossings.
use super::Result;
use crate::config_store::random_secret;
use std::{
    ffi::CString,
    fs::File,
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, PermissionsExt},
    },
    path::{Component, Path},
};

pub(super) struct Root {
    pub file: File,
    pub uid: u32,
    offline: bool,
}
pub(super) struct Data {
    pub bytes: Vec<u8>,
    pub mode: u32,
    pub gid: u32,
}
#[repr(C)]
struct How {
    flags: u64,
    mode: u64,
    resolve: u64,
}
fn name(value: &str) -> Result<CString> {
    CString::new(value).map_err(|_| "Invalid image path")
}
fn open(dir: &File, value: &str, flags: i32, mode: u32, offline: bool) -> Result<Option<File>> {
    let value = name(value)?;
    let how = How {
        flags: (flags | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK) as u64,
        mode: u64::from(mode),
        resolve: if offline {
            0x01 | 0x04 | 0x08
        } else {
            0x04 | 0x08
        },
    };
    // SAFETY: stable Linux openat2 UAPI; all pointers live across the syscall.
    let fd = unsafe {
        libc::syscall(
            libc::SYS_openat2,
            dir.as_raw_fd(),
            value.as_ptr(),
            &how,
            std::mem::size_of::<How>(),
        )
    } as i32;
    if fd < 0 {
        return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
            Ok(None)
        } else {
            Err("Unsafe or unavailable offline path (Linux openat2 required)")
        };
    }
    Ok(Some(unsafe { File::from_raw_fd(fd) }))
}
fn sync(dir: &File) -> Result<()> {
    dir.sync_all().map_err(|_| "Cannot sync offline directory")
}
impl Root {
    pub fn live() -> Result<Self> {
        if unsafe { libc::geteuid() } != 0 {
            return Err("Live deployment requires root");
        }
        let file = File::open("/").map_err(|_| "Cannot open live root")?;
        let meta = file.metadata().map_err(|_| "Cannot inspect live root")?;
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err("Unsafe live root");
        }
        Ok(Self {
            file,
            uid: 0,
            offline: false,
        })
    }
    pub fn open(path: &Path, uid: u32) -> Result<Self> {
        if !path.is_absolute()
            || path
                .components()
                .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
        {
            return Err("Supply an absolute normalized offline root");
        }
        let mut directory = File::open("/").map_err(|_| "Cannot inspect filesystem root")?;
        let live = directory
            .metadata()
            .map_err(|_| "Cannot inspect live root")?;
        let components: Vec<_> = path
            .components()
            .filter_map(|c| {
                if let Component::Normal(v) = c {
                    Some(v)
                } else {
                    None
                }
            })
            .collect();
        for component in &components {
            let component =
                CString::new(component.as_encoded_bytes()).map_err(|_| "Invalid root path")?;
            let fd = unsafe {
                libc::openat(
                    directory.as_raw_fd(),
                    component.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err("Offline root ancestors must be real directories");
            }
            directory = unsafe { File::from_raw_fd(fd) };
            let meta = directory
                .metadata()
                .map_err(|_| "Cannot inspect root ancestor")?;
            if ![0, uid].contains(&meta.uid())
                || meta.mode() & 0o022 != 0 && !(meta.uid() == 0 && meta.mode() & 0o1000 != 0)
            {
                return Err("Unsafe root ancestor ownership or permissions");
            }
        }
        let meta = directory
            .metadata()
            .map_err(|_| "Cannot inspect offline root")?;
        if (meta.dev(), meta.ino()) == (live.dev(), live.ino()) {
            return Err("Live-root deployment is disabled; supply an offline image");
        }
        if meta.uid() != uid || meta.mode() & 0o022 != 0 {
            return Err("Offline root must be protected and owned by deployment user");
        }
        Ok(Self {
            file: directory,
            uid,
            offline: true,
        })
    }
    pub fn identity(&self) -> Result<(u64, u64)> {
        let m = self
            .file
            .metadata()
            .map_err(|_| "Cannot inspect image identity")?;
        Ok((m.dev(), m.ino()))
    }
    pub fn dir(&self, path: &str, create: bool) -> Result<Option<File>> {
        let mut directory = self
            .file
            .try_clone()
            .map_err(|_| "Cannot clone image directory")?;
        if path.is_empty() {
            return Ok(Some(directory));
        }
        for part in path.split('/') {
            if part.is_empty() || part == "." || part == ".." {
                return Err("Invalid relative image path");
            }
            let next = match open(
                &directory,
                part,
                libc::O_RDONLY | libc::O_DIRECTORY,
                0,
                self.offline,
            )? {
                Some(next) => next,
                None if create => {
                    let part = name(part)?;
                    if unsafe { libc::mkdirat(directory.as_raw_fd(), part.as_ptr(), 0o755) } != 0 {
                        return Err("Cannot create offline directory");
                    }
                    sync(&directory)?;
                    let created = open(
                        &directory,
                        part.to_str().map_err(|_| "Invalid directory name")?,
                        libc::O_RDONLY | libc::O_DIRECTORY,
                        0,
                        self.offline,
                    )?
                    .ok_or("Created directory disappeared")?;
                    created
                        .set_permissions(std::fs::Permissions::from_mode(0o755))
                        .map_err(|_| "Cannot set image directory permissions")?;
                    sync(&created)?;
                    created
                }
                None => return Ok(None),
            };
            let m = next
                .metadata()
                .map_err(|_| "Cannot inspect image directory")?;
            if m.uid() != self.uid || m.mode() & 0o022 != 0 {
                return Err("Unsafe image directory ownership or permissions");
            }
            directory = next;
        }
        Ok(Some(directory))
    }
    fn parent(&self, path: &str, create: bool) -> Result<Option<(File, String)>> {
        let (parent, file) = path.rsplit_once('/').unwrap_or(("", path));
        if file.is_empty() || file == "." || file == ".." {
            return Err("Invalid image filename");
        }
        Ok(self.dir(parent, create)?.map(|dir| (dir, file.to_owned())))
    }
    pub fn read(&self, path: &str, limit: usize) -> Result<Option<Data>> {
        let Some((parent, file)) = self.parent(path, false)? else {
            return Ok(None);
        };
        let Some(mut handle) = open(&parent, &file, libc::O_RDONLY, 0, self.offline)? else {
            return Ok(None);
        };
        let m = handle
            .metadata()
            .map_err(|_| "Cannot inspect deployment file")?;
        if !m.is_file()
            || m.uid() != self.uid
            || m.nlink() != 1
            || m.mode() & 0o7022 != 0
            || m.len() > limit as u64
        {
            return Err("Unsafe or oversized deployment file");
        }
        // Restoring only bytes/mode must not silently discard ACLs/capabilities/labels.
        if unsafe { libc::flistxattr(handle.as_raw_fd(), std::ptr::null_mut(), 0) } != 0 {
            return Err("Extended file metadata needs distribution-specific deployment");
        }
        let mut bytes = Vec::new();
        std::io::Read::by_ref(&mut handle)
            .take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read deployment file")?;
        if bytes.len() > limit {
            return Err("Deployment file exceeds limit");
        }
        Ok(Some(Data {
            bytes,
            mode: m.mode() & 0o7777,
            gid: m.gid(),
        }))
    }
    pub fn write(&self, path: &str, data: &Data) -> Result<()> {
        let (parent, file) = self.parent(path, true)?.ok_or("Missing image parent")?;
        let temporary = format!(".relayforge-{}", random_secret()?);
        let mut handle = open(
            &parent,
            &temporary,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            0o600,
            self.offline,
        )?
        .ok_or("Cannot create transaction temporary")?;
        let temp = name(&temporary)?;
        let result = (|| {
            handle
                .write_all(&data.bytes)
                .map_err(|_| "Cannot write deployment file")?;
            if unsafe { libc::fchown(handle.as_raw_fd(), self.uid, data.gid) } != 0 {
                return Err("Cannot restore deployment ownership");
            }
            handle
                .set_permissions(std::fs::Permissions::from_mode(data.mode))
                .map_err(|_| "Cannot set deployment permissions")?;
            handle
                .sync_all()
                .map_err(|_| "Cannot sync deployment file")?;
            let file = name(&file)?;
            if unsafe {
                libc::renameat(
                    parent.as_raw_fd(),
                    temp.as_ptr(),
                    parent.as_raw_fd(),
                    file.as_ptr(),
                )
            } != 0
            {
                return Err("Cannot publish deployment file");
            }
            sync(&parent)
        })();
        if result.is_err() {
            unsafe { libc::unlinkat(parent.as_raw_fd(), temp.as_ptr(), 0) };
        }
        result
    }
    pub fn remove(&self, path: &str) -> Result<()> {
        let Some((parent, file)) = self.parent(path, false)? else {
            return Ok(());
        };
        let file = name(&file)?;
        if unsafe { libc::unlinkat(parent.as_raw_fd(), file.as_ptr(), 0) } != 0
            && std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOENT)
        {
            return Err("Cannot retire deployment file");
        }
        sync(&parent)
    }
    pub fn sync_path(&self, path: &str) -> Result<()> {
        let Some((parent, file)) = self.parent(path, false)? else {
            return Ok(());
        };
        if let Some(handle) = open(&parent, &file, libc::O_RDONLY, 0, self.offline)? {
            handle
                .sync_all()
                .map_err(|_| "Cannot sync recovered deployment file")?;
        }
        sync(&parent)
    }
    pub fn lock(&self, path: &str) -> Result<File> {
        let (parent, file) = self.parent(path, true)?.ok_or("Missing lock parent")?;
        let handle = open(
            &parent,
            &file,
            libc::O_RDWR | libc::O_CREAT,
            0o600,
            self.offline,
        )?
        .ok_or("Cannot open deployment lock")?;
        let m = handle
            .metadata()
            .map_err(|_| "Cannot inspect deployment lock")?;
        if !m.is_file() || m.uid() != self.uid || m.nlink() != 1 || m.mode() & 0o7777 != 0o600 {
            return Err("Unsafe deployment lock");
        }
        if unsafe { libc::flock(handle.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("Deployment or backend is already locked");
        }
        sync(&parent)?;
        Ok(handle)
    }
    pub fn entries(&self, path: &str) -> Result<Vec<String>> {
        let Some(directory) = self.dir(path, false)? else {
            return Ok(vec![]);
        };
        // This proc path refers only to our pinned descriptor, never caller input.
        std::fs::read_dir(format!("/proc/self/fd/{}", directory.as_raw_fd()))
            .map_err(|_| "Cannot inspect runtime directory")?
            .map(|entry| {
                entry
                    .map_err(|_| "Cannot inspect runtime entry")
                    .and_then(|entry| {
                        entry
                            .file_name()
                            .into_string()
                            .map_err(|_| "Invalid runtime filename")
                    })
            })
            .collect()
    }
    pub fn private_dir(&self, path: &str) -> Result<()> {
        let dir = self
            .dir(path, true)?
            .ok_or("Missing transaction directory")?;
        let m = dir
            .metadata()
            .map_err(|_| "Cannot inspect state directory")?;
        // Newly made directories are empty; never tighten an existing populated directory.
        if m.mode() & 0o7777 != 0o700 {
            if !self.entries(path)?.is_empty() {
                return Err("Deployment state directory must be private");
            }
            dir.set_permissions(std::fs::Permissions::from_mode(0o700))
                .map_err(|_| "Cannot protect state directory")?;
            sync(&dir)?;
        }
        Ok(())
    }
}
