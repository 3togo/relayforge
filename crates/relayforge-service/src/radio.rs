//! Bounded, read-only iw inspection. Dropping an inspection kills its process group.
use relayforge_core::radio::{device, Capture, Snapshot, MAX_CAPTURE};
use std::os::unix::process::CommandExt;
use std::{path::Path, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::{Child, Command},
    time::timeout,
};

struct Job {
    child: Child,
    group: i32,
    armed: bool,
}
impl Drop for Job {
    fn drop(&mut self) {
        if self.armed {
            // SAFETY: the child starts a new process group; its unreaped PID
            // cannot be reused while this guard owns the Child.
            unsafe {
                libc::kill(-self.group, libc::SIGKILL);
            }
        }
    }
}
async fn drain(mut pipe: impl AsyncRead + Unpin) -> Result<Vec<u8>, &'static str> {
    let mut bytes = Vec::new();
    (&mut pipe)
        .take((MAX_CAPTURE + 1) as u64)
        .read_to_end(&mut bytes)
        .await
        .map_err(|_| "Cannot read radio inspection output")?;
    if bytes.len() > MAX_CAPTURE {
        return Err("Radio inspection output exceeds limit");
    }
    Ok(bytes)
}
pub(crate) async fn command(
    program: &Path,
    args: &[&str],
    deadline: Duration,
) -> Result<String, &'static str> {
    command_env(program, args, deadline, None).await
}
pub(crate) async fn command_env(
    program: &Path,
    args: &[&str],
    deadline: Duration,
    environment: Option<&std::collections::BTreeMap<std::ffi::OsString, std::ffi::OsString>>,
) -> Result<String, &'static str> {
    let mut command = Command::new(program);
    if let Some(environment) = environment {
        command.env_clear().envs(environment);
    }
    command
        .args(args)
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    command.as_std_mut().process_group(0);
    let mut child = command
        .spawn()
        .map_err(|_| "Cannot execute radio inspection utility")?;
    let stdout = child.stdout.take().ok_or("Missing radio output pipe")?;
    let stderr = child.stderr.take().ok_or("Missing radio diagnostic pipe")?;
    let mut job = Job {
        group: child.id().ok_or("Missing radio inspection process")? as i32,
        child,
        armed: true,
    };
    let operation = async {
        // Drain both pipes before reaping: inherited descriptors are bounded by
        // the deadline, and cancellation can still kill the owned process group.
        let (stdout, _) = tokio::try_join!(drain(stdout), drain(stderr))?;
        let status = job
            .child
            .wait()
            .await
            .map_err(|_| "Cannot wait for radio inspection")?;
        job.armed = false;
        if !status.success() {
            return Err("Radio inspection command failed");
        }
        String::from_utf8(stdout).map_err(|_| "Radio inspection output is not UTF-8")
    };
    timeout(deadline, operation)
        .await
        .map_err(|_| "Radio inspection timed out")?
}

pub enum Source {
    System,
    Demo,
    Replay(Box<Capture>),
}
impl Source {
    pub async fn inspect(&self, iface: &str) -> Result<Snapshot, &'static str> {
        match self {
            Self::Demo => Snapshot::parse(&Capture::demo(iface)),
            Self::Replay(record) => {
                if record.wifi_iface != iface {
                    return Err("Radio replay adapter differs from configuration");
                }
                Snapshot::parse(record)
            }
            Self::System => {
                // Validate before passing an interface to iw, including option-like names.
                relayforge_core::config::parse_patch(
                    &serde_json::json!({"WIFI_IFACE":iface}).to_string(),
                )?;
                let program = ["/usr/sbin/iw", "/usr/bin/iw", "/sbin/iw", "/bin/iw"]
                    .into_iter()
                    .map(Path::new)
                    .find(|p| p.is_file())
                    .ok_or("Radio inspection utility is unavailable")?;
                let inspect = async {
                    let info =
                        command(program, &["dev", iface, "info"], Duration::from_secs(2)).await?;
                    let (phy, _) = device(&info, iface)?;
                    let phy = command(
                        program,
                        &["phy", &format!("phy{phy}"), "info"],
                        Duration::from_secs(2),
                    )
                    .await?;
                    let link =
                        command(program, &["dev", iface, "link"], Duration::from_secs(2)).await?;
                    Snapshot::parse(&Capture {
                        wifi_iface: iface.into(),
                        info,
                        phy,
                        link,
                    })
                };
                timeout(Duration::from_secs(5), inspect)
                    .await
                    .map_err(|_| "Radio inspection timed out")?
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn captures_output_and_sanitizes_failed_diagnostics() {
        assert_eq!(
            command(Path::new("/bin/echo"), &["hello"], Duration::from_secs(1))
                .await
                .unwrap(),
            "hello\n"
        );
        assert_eq!(
            command(
                Path::new("/bin/sh"),
                &["-c", "echo private-network >&2; exit 1"],
                Duration::from_secs(1)
            )
            .await
            .unwrap_err(),
            "Radio inspection command failed"
        );
    }
    #[tokio::test]
    async fn invalid_encoding_and_diagnostic_floods_fail_closed() {
        assert_eq!(
            command(
                Path::new("/usr/bin/python3"),
                &["-c", "import os; os.write(1, bytes([255]))"],
                Duration::from_secs(2)
            )
            .await
            .unwrap_err(),
            "Radio inspection output is not UTF-8"
        );
        assert_eq!(
            command(
                Path::new("/bin/sh"),
                &["-c", "yes private-diagnostic >&2"],
                Duration::from_secs(2)
            )
            .await
            .unwrap_err(),
            "Radio inspection output exceeds limit"
        );
    }
    #[tokio::test]
    async fn cancellation_kills_inspection_and_its_descendants() {
        let path = std::env::temp_dir().join(format!(
            "relayforge-radio-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&path).unwrap();
        let directory = path.clone();
        let task = tokio::spawn(async move {
            command(
                Path::new("/bin/sh"),
                &[
                    "-c",
                    "echo $$ > \"$1/parent\"; sleep 30 & echo $! > \"$1/child\"; wait",
                    "radio-test",
                    directory.to_str().unwrap(),
                ],
                Duration::from_secs(10),
            )
            .await
        });
        timeout(Duration::from_secs(3), async {
            while !["parent", "child"].iter().all(|name| {
                std::fs::read_to_string(path.join(name))
                    .is_ok_and(|text| text.trim().parse::<u32>().is_ok())
            }) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let pids: Vec<u32> = ["parent", "child"]
            .iter()
            .map(|name| {
                std::fs::read_to_string(path.join(name))
                    .unwrap()
                    .trim()
                    .parse()
                    .unwrap()
            })
            .collect();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        timeout(Duration::from_secs(3), async {
            loop {
                // A killed descendant can briefly remain a zombie until its new
                // parent reaps it; it must no longer execute or hold pipes open.
                if pids.iter().all(|pid| {
                    std::fs::read_to_string(format!("/proc/{pid}/stat")).map_or(true, |stat| {
                        stat.split_once(") ")
                            .is_some_and(|(_, tail)| tail.starts_with('Z'))
                    })
                }) {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        std::fs::remove_dir_all(path).unwrap();
    }
    #[tokio::test]
    async fn bounds_output_and_inherited_pipe_lifetime() {
        assert_eq!(
            command(
                Path::new("/bin/sh"),
                &["-c", "yes x"],
                Duration::from_secs(2)
            )
            .await
            .unwrap_err(),
            "Radio inspection output exceeds limit"
        );
        assert_eq!(
            command(
                Path::new("/bin/sh"),
                &["-c", "sleep 30 & wait"],
                Duration::from_millis(100)
            )
            .await
            .unwrap_err(),
            "Radio inspection timed out"
        );
    }
}
