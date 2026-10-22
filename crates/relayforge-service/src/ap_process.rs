//! PID-fd signalling with boot/start-time and exact argv checks. Never signal a raw pidfile PID.
use crate::nm_journal::Result;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File},
    io::Read,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::MetadataExt,
    },
    path::Path,
};
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Birth {
    pub pid: u32,
    pub start: String,
    pub boot: String,
}
fn boot() -> Result<String> {
    fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .map(|v| v.trim().to_owned())
        .map_err(|_| "Cannot inspect kernel boot identity")
}
fn stat(pid: u32) -> Result<Option<(String, bool)>> {
    let text = match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("Cannot inspect process identity"),
    };
    let (_, fields) = text.rsplit_once(')').ok_or("Malformed process identity")?;
    let fields: Vec<_> = fields.split_whitespace().collect();
    if fields.len() < 20 || !fields[19].bytes().all(|b| b.is_ascii_digit()) {
        return Err("Malformed process start time");
    }
    Ok(Some((fields[19].into(), fields[0] == "Z")))
}
impl Birth {
    pub fn capture(pid: u32) -> Result<Self> {
        let (start, zombie) = stat(pid)?.ok_or("Child exited before recording its identity")?;
        if pid <= 1 || zombie {
            return Err("Invalid live child identity");
        }
        Ok(Self {
            pid,
            start,
            boot: boot()?,
        })
    }
    pub fn validate(&self) -> Result<()> {
        if self.pid <= 1
            || self.pid > i32::MAX as u32
            || self.start.is_empty()
            || self.start.len() > 24
            || !self.start.bytes().all(|b| b.is_ascii_digit())
            || self.boot.len() != 36
            || !self
                .boot
                .bytes()
                .all(|b| b.is_ascii_hexdigit() || b == b'-')
        {
            return Err("Invalid process birth record");
        }
        Ok(())
    }
    pub fn live(&self) -> Result<bool> {
        if self.boot != boot()? {
            return Ok(false);
        }
        Ok(stat(self.pid)?.is_some_and(|(start, zombie)| start == self.start && !zombie))
    }
}
pub struct Handle {
    fd: File,
    birth: Birth,
}
impl Handle {
    pub fn open(birth: &Birth) -> Result<Option<Self>> {
        birth.validate()?;
        if !birth.live()? {
            return Ok(None);
        }
        let raw = unsafe { libc::syscall(libc::SYS_pidfd_open, birth.pid, 0u32) };
        if raw < 0 {
            return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                Ok(None)
            } else {
                Err("Cannot open owned process handle")
            };
        }
        let fd = unsafe { File::from_raw_fd(raw as i32) };
        if !birth.live()? {
            return Ok(None);
        }
        Ok(Some(Self {
            fd,
            birth: birth.clone(),
        }))
    }
    pub fn exited(&self) -> Result<bool> {
        let mut fd = libc::pollfd {
            fd: self.fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let code = unsafe { libc::poll(&mut fd, 1, 0) };
        if code < 0 {
            return Err("Cannot inspect owned process handle");
        }
        Ok(code > 0)
    }
    pub fn verify(&self, allowed: &[(String, Vec<String>)]) -> Result<bool> {
        if self.exited()? || !self.birth.live()? {
            return Ok(false);
        }
        let path = format!("/proc/{}", self.birth.pid);
        if fs::metadata(&path)
            .map_err(|_| "Cannot inspect child owner")?
            .uid()
            != unsafe { libc::geteuid() }
        {
            return Err("Owned process user changed");
        }
        let exe = fs::read_link(format!("{path}/exe"))
            .map_err(|_| "Cannot inspect child executable")?
            .to_string_lossy()
            .trim_end_matches(" (deleted)")
            .to_owned();
        let mut bytes = Vec::new();
        File::open(format!("{path}/cmdline"))
            .and_then(|f| f.take(16385).read_to_end(&mut bytes))
            .map_err(|_| "Cannot inspect child arguments")?;
        if bytes.len() > 16384 {
            return Err("Oversized child arguments");
        }
        let text = String::from_utf8(bytes).map_err(|_| "Invalid child arguments")?;
        let args: Vec<_> = text
            .trim_end_matches('\0')
            .split('\0')
            .map(str::to_owned)
            .collect();
        if !allowed.iter().any(|(e, a)| *e == exe && *a == args) {
            return Err("Owned process command changed; refusing signal");
        }
        Ok(true)
    }
    pub fn signal(&self, signal: i32, allowed: &[(String, Vec<String>)]) -> Result<()> {
        if !self.verify(allowed)? {
            return Ok(());
        }
        let code = unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.fd.as_raw_fd(),
                signal,
                std::ptr::null::<libc::siginfo_t>(),
                0u32,
            )
        };
        if code < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            return Err("Cannot signal owned process");
        }
        Ok(())
    }
}
pub fn trusted_file(path: &Path) -> Result<String> {
    let meta = fs::symlink_metadata(path).map_err(|_| "Cannot inspect helper installation")?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o022 != 0 {
        return Err("Unsafe helper installation");
    }
    path.canonicalize()
        .map_err(|_| "Cannot resolve helper installation")?
        .to_str()
        .map(str::to_owned)
        .ok_or("Invalid helper installation path")
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reused_birth_and_wrong_argv_are_never_signalled() {
        let birth = Birth::capture(std::process::id()).unwrap();
        let handle = Handle::open(&birth).unwrap().unwrap();
        assert!(handle
            .signal(libc::SIGUSR1, &[("/unrelated".into(), vec![])])
            .is_err());
        let mut reused = birth;
        reused.start.push('0');
        assert!(Handle::open(&reused).unwrap().is_none());
    }
}
